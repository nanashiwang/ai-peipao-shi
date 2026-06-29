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


if __name__ == "__main__":
    unittest.main()
