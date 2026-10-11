import re
from fastapi.testclient import TestClient


def test_panel_pages_and_settings():
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/login", data={"password": "testpass123"}, follow_redirects=False).status_code == 303
        for path in ("/", "/settings", "/agent", "/users", "/payments"):
            r = c.get(path)
            assert r.status_code == 200, path
        page = c.get("/settings").text
        assert "قوانین مدیر برای دستیار" in page and "agent_rules" in page
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        r = c.post("/settings", follow_redirects=True, data={
            "csrf": csrf, "admin_ids": "1", "trial_days": "3", "plans": "30:150000", "ai_provider": "anthropic",
            "ai_api_key": "sk-test-1234", "agent_enabled": "1", "agent_rules": "قمار ممنوع", "agent_blocked_words": "شرط‌بندی",
            "agent_max_msgs_day": "30", "agent_max_auto_day": "5", "agent_max_sources": "8", "agent_violation_limit": "4"})
        assert r.status_code == 200
        s = c.get("/settings").text
        assert "••••1234" in s and "قمار ممنوع" in s and 'value="30"' in s          # key stored (masked), rules persisted
        assert "sk-test-1234" not in s                                              # secret never echoed back
        # bad number is rejected
        bad = c.post("/settings", follow_redirects=True, data={
            "csrf": csrf, "admin_ids": "1", "trial_days": "3", "plans": "30:150000", "agent_max_msgs_day": "abc"})
        assert "باید عدد باشند" in bad.text
        a = c.get("/agent").text
        assert "کلید ثبت شده" in a and "تا الان موردی ثبت نشده" in a
        t = c.post("/agent/test", data={"csrf": csrf}, follow_redirects=True)       # fake key + no network -> clean error flash
        assert t.status_code == 200 and ("خطا در اتصال" in t.text or "اتصال برقرار" in t.text)
