"""Отписка от рассылок по заголовку List-Unsubscribe (RFC 2369 / RFC 8058).

Два способа, оба без открытия страниц и без ссылок из текста письма:
  * mailto: — письмо на адрес отписки с того ящика, куда пришла рассылка;
  * https + List-Unsubscribe-Post: List-Unsubscribe=One-Click — один POST-запрос,
    редиректы не выполняются, внутренние/локальные адреса запрещены.
Ссылка https без заявленного One-Click — это страница с кнопками, её не трогаем.
"""
import ipaddress
import logging
import re
import smtplib
import socket
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

log = logging.getLogger("unsub")
URI_RE = re.compile(r"<\s*([^>]+?)\s*>")


def parse_header(value: str):
    """-> (mailto-ссылки, https-ссылки) из List-Unsubscribe."""
    mailtos, https = [], []
    for uri in URI_RE.findall(value or ""):
        low = uri.lower()
        if low.startswith("mailto:"):
            mailtos.append(uri)
        elif low.startswith("https://"):
            https.append(uri)
    return mailtos, https


def one_click_allowed(post_header: str) -> bool:
    return "list-unsubscribe=one-click" in (post_header or "").replace(" ", "").lower()


def parse_mailto(uri: str):
    """mailto:addr?subject=..&body=.. -> (addr, subject, body)"""
    rest = uri[len("mailto:"):]
    addr, _, query = rest.partition("?")
    params = urllib.parse.parse_qs(query)
    addr = urllib.parse.unquote(addr).strip()
    subject = (params.get("subject") or ["unsubscribe"])[0]
    body = (params.get("body") or ["unsubscribe"])[0]
    return addr, subject, body


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return bool(infos)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def post_one_click(url: str, timeout=20) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    if not _public_host(host):
        raise ValueError(f"адрес {host} не публичный — не трогаю")
    req = urllib.request.Request(url, data=b"List-Unsubscribe=One-Click", method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "User-Agent": "mail-sorter-unsubscribe"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as r:
            return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code < 400:
            return f"HTTP {e.code}"
        raise RuntimeError(f"HTTP {e.code}")


def send_mailto(uri, mailbox, password, smtp_host, smtp_port, timeout=60) -> str:
    addr, subject, body = parse_mailto(uri)
    if "@" not in addr:
        raise ValueError("кривой mailto")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mailbox
    msg["To"] = addr
    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout) as s:
        s.login(mailbox, password)
        s.sendmail(mailbox, [addr], msg.as_string())
    return f"письмо на {addr}"


def unsubscribe(row, settings, post=post_one_click, mail=send_mailto) -> tuple[bool, str]:
    """row: header, post, mailbox. Возвращает (получилось ли хоть одним способом, описание)."""
    mailtos, https = parse_header(row["header"])
    done, notes = [], []
    pw = settings.mailboxes.get(row["mailbox"])
    for uri in mailtos[:1]:
        if not pw:
            notes.append("mailto: нет пароля ящика")
            continue
        try:
            done.append(mail(uri, row["mailbox"], pw, settings.smtp_host, settings.smtp_port))
        except Exception as e:
            notes.append(f"mailto: ошибка {e}")
    if https and one_click_allowed(row["post"]):
        try:
            done.append("запрос " + post(https[0]))
        except Exception as e:
            notes.append(f"запрос: ошибка {e}")
    elif https:
        notes.append("ссылка без one-click — пропущена")
    if not mailtos and not https:
        notes.append("в заголовке нет поддерживаемых способов")
    return bool(done), "; ".join(done + notes)
