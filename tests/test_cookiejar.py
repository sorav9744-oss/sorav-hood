import time

from joshiclip import cookiejar as cj

JAR = (".youtube.com\tTRUE\t/\tTRUE\t{exp}\t__Secure-1PSID\tabc\n"
       ".youtube.com\tTRUE\t/\tTRUE\t{far}\tPREF\tx\n")


def test_roundtrip_and_wrong_secret():
    blob = cj.encrypt(b"youtube cookies", "secret-A")
    assert cj.decrypt(blob, "secret-A") == b"youtube cookies"
    assert cj.decrypt(blob, "secret-B") is None                  # a refreshed secret never reads an old jar
    assert cj.decrypt(b"JCK1" + bytes(60), "secret-A") is None  # damaged


def test_refreshed_jar_used_only_for_the_same_secret(tmp_path):
    cache, out = tmp_path / "c", tmp_path / "jar.txt"
    assert cj.load("old secret youtube", cache, out) == "secret"
    out.write_text("refreshed youtube jar" + "x" * 200)
    assert cj.save("old secret youtube", out, cache)
    assert cj.load("old secret youtube", cache, out) == "refreshed" and out.read_text().startswith("refreshed")
    assert cj.load("NEW secret youtube", cache, out) == "secret" and out.read_text() == "NEW secret youtube"


def test_days_left_uses_login_cookies_only():
    now = time.time()
    txt = JAR.format(exp=int(now + 5 * 86400), far=int(now + 400 * 86400))
    assert 4.9 < cj.days_left(txt, now) < 5.1
    assert cj.days_left("# nothing\n", now) is None


def test_media_names_are_safe_and_spans_match():
    from joshiclip import mediastore as ms
    assert ms.clip_name("awYJWalvK_0", 12.345, 56.7) == "clip_awYJWalvK_0_12.35_56.70.mkv"
    assert ms.safe("a b/c?.mkv") == "a_b_c_.mkv"
    assert not ms.enabled({"daily": {}})
