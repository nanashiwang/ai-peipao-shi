import unittest
from html.parser import HTMLParser
from pathlib import Path


class SidebarParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.panel_ids = []
        self.nav_tabs = []
        self.nav_titles = []
        self.active_tabs = []
        self.selects = {}

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "section" and "panel" in data.get("class", "").split():
            self.panel_ids.append(data.get("id", ""))
        if tag == "button" and data.get("data-tab"):
            tab = data["data-tab"]
            self.nav_tabs.append(tab)
            self.nav_titles.append(data.get("data-title", ""))
            if "active" in data.get("class", "").split():
                self.active_tabs.append(tab)
        if tag == "select" and data.get("id"):
            self.selects[data["id"]] = data


class SidebarNavigationTest(unittest.TestCase):
    def setUp(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        self.parser = SidebarParser()
        self.parser.feed(html)

    def test_every_panel_has_sidebar_entry(self):
        self.assertEqual(sorted(self.parser.panel_ids), sorted(self.parser.nav_tabs))

    def test_sidebar_tabs_are_unique_and_titled(self):
        self.assertEqual(len(self.parser.nav_tabs), len(set(self.parser.nav_tabs)))
        self.assertTrue(all(title.strip() for title in self.parser.nav_titles))

    def test_default_active_tab_matches_default_panel(self):
        self.assertEqual(self.parser.active_tabs, ["dashboard"])

    def test_operations_boards_expose_campus_filters(self):
        self.assertEqual(self.parser.selects["campusFilter"].get("onchange"), "setCampusFilter(this.value)")
        self.assertEqual(self.parser.selects["adminCampusFilter"].get("onchange"), "setCampusFilter(this.value)")

    def test_frontend_passes_campus_scope_to_operations_apis(self):
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        self.assertIn('localStorage.getItem("campusFilter")', js)
        self.assertIn('query.set("campus_name", state.selectedCampusName)', js)
        self.assertIn('scopedPath("/api/workbench/overview", { limit: 8 }, { includeCoach: true })', js)
        self.assertIn('scopedPath("/api/workbench/today-priorities", { limit: 12 })', js)
        self.assertIn('scopedPath("/api/admin/service-quality")', js)

    def test_reply_page_exposes_auto_draft_action(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("自动生成待审回复", html)
        self.assertIn("autoDraftReplies()", html)
        self.assertIn("/api/agent/replies/auto-draft", js)

    def test_control_auth_page_exposes_login_and_first_admin_registration(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="authGate"', html)
        self.assertNotIn('data-tab="auth"', html)
        self.assertIn('class="auth-only"', html)
        self.assertIn("先登录，再进入总控台", html)
        self.assertIn("控制端登录", html)
        self.assertIn("首次注册会自动成为超管", html)
        self.assertIn("setAuthGateVisible", js)
        self.assertIn("/api/admin/auth/status", js)
        self.assertIn("/api/admin/auth/register", js)
        self.assertIn("/api/admin/auth/login", js)


if __name__ == "__main__":
    unittest.main()
