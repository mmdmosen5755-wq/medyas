from datetime import datetime
from app import scraper, policy, curator
from app.db import Candidate, AgentProfile
from fixtures import PAGE


def test_parse_lines():
    ok, bad = scraper.parse_handle_lines("""@health_ch
https://eitaa.com/news.daily/123
eitaa.com/s/Tech_Ch
https://eitaa.com/joinchat/abcdef
health_ch
12
ab""")
    assert [h for _, h in ok] == ["health_ch", "news.daily", "Tech_Ch"]   # deduped, post-link & /s/ handled
    assert len(bad) == 3                                                  # invite link, digits-only, too short


def test_parse_lines_persian_digits_and_commas():
    ok, _ = scraper.parse_handle_lines("chan_۱،chan_2 ; chan_۳")
    assert [h for _, h in ok] == ["chan_1", "chan_2", "chan_3"]


def test_parse_page():
    info = scraper.parse_page(PAGE, "health")
    assert info.exists and info.kind == "channel"
    assert info.title == "کانال سلامت" and info.subscribers == "12.5K"
    assert [p.msg_id for p in info.posts] == [101, 102]
    p = info.posts[0]
    assert "متن اول\nخط دوم" in p.text and "(https://example.com/x)" in p.text and "#سلامت" in p.text
    assert p.image_url == "https://eitaa.com/file/abc.jpg" and p.views == 1200
    assert p.posted_at == datetime(2026, 10, 10, 5, 0)                    # +03:30 -> UTC
    assert info.posts[1].has_media and info.posts[1].views == 3000


def test_parse_missing_channel():
    info = scraper.parse_page("<html><head><title>Eitaa</title></head></html>", "nope")
    assert info.exists is False


def test_ad_signals():
    s, f = scraper.ad_signals("سفارش تبلیغ در این کانال: تماس 09123456789 @ads")
    assert s >= 85 and f
    s2, _ = scraper.ad_signals("دانشمندان ایرانی موفق به کشف روش جدیدی برای درمان دیابت شدند.")
    assert s2 == 0
    s3, _ = scraper.ad_signals("تخفیف ویژه و حراج بزرگ؛ همین امروز بخرید")
    assert 0 < s3 < 85            # soft signals alone never auto-reject: the AI decides


def test_similar_and_hash():
    a = "امروز قیمت طلا ۲۰ هزار تومان افزایش یافت و بازار واکنش نشان داد #طلا @chan"
    b = "امروز قیمت طلا 20 هزار تومان افزایش یافت و بازار واکنش نشان داد"
    assert scraper.content_hash(a.split("#")[0]) == scraper.content_hash(b) or scraper.similar(a, b)
    assert not scraper.similar(a, "آموزش پخت کیک اسفنجی با دستور ساده برای مهمانی")


def test_blocked_words_normalization():
    r = policy.Rules(blocked=(policy.normalize("قمار"), policy.normalize("شرط بندی")))
    assert policy.blocked_word(r, "درباره‌ی ق‌مار صحبت کنیم")           # ZWNJ inside the word
    assert policy.blocked_word(r, "سایت شرط‌بندی") and policy.blocked_word(r, "شرطبندی")
    assert not policy.blocked_word(r, "ورزش و سلامت")


def test_clean_tail_and_compose():
    t = "خبر مهم امروز\nجزئیات بیشتر در متن\n\n🆔 @source_chan\n➖➖➖\nhttps://eitaa.com/source_chan"
    assert curator.clean_tail(t) == "خبر مهم امروز\nجزئیات بیشتر در متن"
    c = Candidate(text=t, rewritten="")
    p = AgentProfile(user_id=1, footer="کانال ما", attribution=True)
    out = curator.compose(c, p, "source_chan", policy.Rules())
    assert out.endswith("کانال ما\n\nمنبع: @source_chan") and "🆔" not in out


def test_extract_json_shapes():
    from app import ai
    assert ai.extract_json('```json\n[{"id":1}]\n```') == [{"id": 1}]          # one-element array stays an array
    assert ai.extract_json('بفرمایید: [{"id":1},{"id":2}] تمام') == [{"id": 1}, {"id": 2}]
    assert ai.extract_json('{"allowed": false}') == {"allowed": False}


def test_database_url_variants():
    from app.config import _prepare_db
    u, a = _prepare_db("postgresql://u:p%40ss@ep-cool-123-pooler.eu-central-1.aws.neon.tech/db?sslmode=require&channel_binding=require")
    assert u.startswith("postgresql+asyncpg://u:p%40ss@ep-cool") and "sslmode" not in u and "channel_binding" not in u
    assert a == {"ssl": True, "statement_cache_size": 0}                      # pooled Neon endpoint
    u, a = _prepare_db("postgres://u:p@db.example.com:5432/x?sslmode=require")
    assert a == {"ssl": True} and u.endswith("/x")
    u, a = _prepare_db("postgresql://u:p@10.0.0.1/x")
    assert a == {} and "+asyncpg" in u                                          # plain internal DB: unchanged behaviour
    u, a = _prepare_db("sqlite+aiosqlite:///./data.db")
    assert a == {} and u == "sqlite+aiosqlite:///./data.db"
