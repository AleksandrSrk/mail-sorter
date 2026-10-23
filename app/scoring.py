"""Оценка «полезности» адреса/домена из спам-фильтра: не попал ли туда кто-то нужный.

Сигналы (от сильного к слабому): мы сами писали на адрес; писали на этот домен;
корпоративный домен; адрес похож на мусор (цифры, случайные буквы)."""
import re

FREE = {
    "mail.ru", "bk.ru", "inbox.ru", "list.ru", "internet.ru", "yandex.ru", "ya.ru", "yandex.com", "rambler.ru",
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com", "me.com",
    "163.com", "126.com", "qq.com", "sina.com", "aol.com", "gmx.de", "gmx.net", "web.de",
}
SUSPICIOUS = 50  # от этого балла — наверх списка с ⚠️


def _junk_local(local: str) -> bool:
    digits = sum(c.isdigit() for c in local)
    return len(local) >= 6 and (digits / len(local) >= 0.5 or bool(re.fullmatch(r"[a-z]{1,3}\d{5,}", local)))


def score_addr(addr: str, by_addr: dict, by_domain: dict) -> tuple[int, list[str]]:
    local, _, domain = addr.partition("@")
    s, why = 0, []
    if by_addr.get(addr):
        s += 100
        why.append(f"мы писали ему {by_addr[addr]} раз")
    elif domain not in FREE and by_domain.get(domain):
        s += 50
        why.append(f"писали на @{domain} {by_domain[domain]} раз")
    if domain not in FREE and domain.endswith((".ru", ".рф", ".su")):
        s += 5
    if _junk_local(local):
        s -= 20
        why.append("адрес похож на мусор")
    return s, why


def score_domain(domain: str, by_domain: dict) -> tuple[int, list[str]]:
    if domain in FREE:
        return -10, ["бесплатная почта — весь домен блокировать нельзя"]
    if by_domain.get(domain):
        return 50, [f"писали на этот домен {by_domain[domain]} раз"]
    return 0, []
