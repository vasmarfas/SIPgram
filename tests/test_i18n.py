import re

from sipgram.messages import LANGUAGES, STRINGS, default_language, fmt_duration, fmt_number, set_language, t
from sipgram.prefs import UserPrefs

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_languages_have_the_same_keys_and_placeholders():
    ru, en = STRINGS["ru"], STRINGS["en"]
    assert set(ru) == set(en), f"only ru: {sorted(set(ru) - set(en))}, only en: {sorted(set(en) - set(ru))}"
    for key in ru:
        assert set(PLACEHOLDER.findall(ru[key])) == set(PLACEHOLDER.findall(en[key])), key


def test_every_string_renders_in_both_languages():
    sample = dict(caller="Иван", account="491", peer="+7 999", duration="01:02", number="101", reason="486 Busy",
                  text="abc", held="Пётр", a="A", b="B", digits="123", n=3, name="line1", lines="491 ✓",
                  gateway="@gw", icon="🟢", username="491", domain="pbx", state="ok", direction="in", bot="@bot",
                  seconds=60, users=2, accounts=1, what="session", room="8000", user="Иван",
                  windows="mon-fri 09:00-18:00", numbers="+7999*", action="busy", title="SIPgram", chat="@team",
                  peers="+7 999, 101", who="@petya", emojis="🐶🍕🚗🎸")
    for lang in LANGUAGES:
        for key in STRINGS[lang]:
            out = t(key, lang, **sample)
            assert out and "{" not in out.replace("{{", ""), f"{lang}/{key}: {out}"


def test_language_is_per_call_and_falls_back():
    assert t("no_active_call", "ru") != t("no_active_call", "en")
    assert t("no_active_call", "de") == t("no_active_call", default_language()), "unknown language falls back"
    set_language("en")
    try:
        assert t("no_active_call") == STRINGS["en"]["no_active_call"], "no explicit language uses the default"
        assert t("no_active_call", "ru") == STRINGS["ru"]["no_active_call"]
    finally:
        set_language("ru")


def test_commands_are_wrapped_in_code_spans():
    """A chat with a user account has no clickable /commands, so they must be tap-to-copy code."""
    for lang in LANGUAGES:
        help_text = STRINGS[lang]["help"]
        for cmd in ("/hangup", "/switch", "/transfer", "/cb", "/redial", "/dnd", "/line", "/history", "/status"):
            assert f"`{cmd}`" in help_text, f"{lang}: {cmd} is not a code span"
        assert help_text.count("`") % 2 == 0 and help_text.count("**") % 2 == 0


def test_markup_from_a_caller_name_cannot_leak():
    out = t("call_ended", "ru", peer="`code` **bold**", duration="00:05")
    assert "`" not in out and "**bold**" not in out and "**00:05**" in out


def test_number_and_duration_formatting():
    assert fmt_number("79991234567") == "+7 999 123-45-67"
    assert fmt_number("101") == "101"
    assert (fmt_duration(65), fmt_duration(3671)) == ("01:05", "1:01:11")


def test_prefs_round_trip(tmp_path):
    path = tmp_path / "prefs.json"
    p = UserPrefs(path)
    assert p.get(1, "lang") is None
    p.set(1, "lang", "en")
    p.set(1, "dnd", True)
    p.set(2, "lang", "ru")
    again = UserPrefs(path)
    assert again.get(1, "lang") == "en" and again.get(1, "dnd") is True and again.get(2, "lang") == "ru"
    assert again.get(3, "lang", "ru") == "ru"


def test_prefs_without_a_file_stay_in_memory():
    p = UserPrefs(None)
    p.set(1, "lang", "en")
    assert p.get(1, "lang") == "en"
