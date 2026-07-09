import unittest
from html.parser import HTMLParser
from pathlib import Path


class SidebarParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.panel_ids = []
        self.nav_tabs = []
        self.subtab_tabs = []
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
        if tag == "button" and data.get("data-subtab"):
            self.subtab_tabs.append(data["data-subtab"])
        if tag == "select" and data.get("id"):
            self.selects[data["id"]] = data


class SidebarNavigationTest(unittest.TestCase):
    def setUp(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        self.parser = SidebarParser()
        self.parser.feed(html)

    def test_every_panel_has_navigation_entry(self):
        reachable_tabs = set(self.parser.nav_tabs + self.parser.subtab_tabs)
        self.assertEqual(sorted(self.parser.panel_ids), sorted(reachable_tabs))

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

    def test_reply_page_exposes_auto_reply_config(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("AI回复配置", html)
        self.assertIn('id="replyConfigPanel"', html)
        self.assertIn("saveReplyAgentConfig", js)
        self.assertIn("/api/agent/reply-config", js)

    def test_agent_config_page_exposes_prompts_and_knowledge_base(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        js = Path("app/static/app.js").read_text(encoding="utf-8")

        self.assertIn('data-tab="agentConfig"', html)
        self.assertIn('id="agentConfigPanel"', html)
        self.assertIn('id="knowledgeForm"', html)
        self.assertIn("saveAgentConfig", js)
        self.assertIn("/api/agent/config", js)
        self.assertIn("/api/agent/knowledge/search", js)

    def test_task_page_exposes_wecom_real_send_action(self):
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("企微试运行（不发送）", js)
        self.assertIn("企微真实发送", js)
        self.assertIn("queueTaskRealSend", js)
        self.assertIn("/real-send", js)

    def test_device_page_exposes_real_send_switch(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        self.assertIn("真实发送开关", html)
        self.assertIn("toggleDeviceRealSend", js)
        self.assertIn("toggleDeviceAnyConversation", js)
        self.assertIn("allow_real_send", js)
        self.assertIn("allow_any_conversation", js)
        self.assertIn("开启真发", js)
        self.assertIn("开启全会话", js)
        self.assertIn("deviceOutboxStatus", js)
        self.assertIn("outbox_pending_count", js)
        self.assertIn("结果补传", js)
        self.assertIn("requestConversationProof", js)
        self.assertIn("queueConversationProof", js)
        self.assertIn("conversation_check_hint", js)
        self.assertIn("下发预检修复校验", js)
        self.assertIn("readiness.actions", js)
        self.assertIn("任务发送前刷新证明", js)
        self.assertIn("requestAllConversationProofs", js)
        self.assertIn("requestMissingConversationProofs", js)
        self.assertIn("missing_only", js)
        self.assertIn("/conversation-checks", js)
        self.assertIn("/conversation-checks/batch", js)
        self.assertIn("conversation_proof_count", js)
        self.assertIn("conversation_proof_total", js)
        self.assertIn("conversation_proof_missing_targets", js)
        self.assertIn("缺失/过期", js)
        self.assertIn("deviceRealSendStats", js)
        self.assertIn("real_send_confirm_rate_24h", js)
        self.assertIn("真发闭环", js)

    def test_control_auth_page_exposes_login_and_first_admin_registration(self):
        html = Path("app/static/index.html").read_text(encoding="utf-8")
        js = Path("app/static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="authGate"', html)
        self.assertNotIn('data-tab="auth"', html)
        self.assertIn('class="view-landing"', html)
        self.assertIn('id="landing"', html)
        self.assertIn("进入控制台", html)
        self.assertIn("showAuth('login')", html)
        self.assertIn("showAuth('register')", html)
        self.assertIn("首次注册会自动成为超管", html)
        self.assertIn("showLanding", js)
        self.assertIn("setAuthGateVisible", js)
        self.assertIn("/api/admin/auth/status", js)
        self.assertIn("/api/admin/auth/register", js)
        self.assertIn("/api/admin/auth/login", js)


if __name__ == "__main__":
    unittest.main()
