import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import (
    FamilyIn,
    SendTaskIn,
    admin_auth_secret,
    create_send_task,
    family_detail,
    list_ai_outputs,
    list_families,
    list_profiles,
    list_reports,
    list_send_logs,
    list_send_tasks,
    sign_admin_token,
    upsert_family,
)
from app.models import AIOutput, Family, ParentProfile, SendLog, SendTask, WeeklyReport


def scoped_request(role: str = "coach", username: str = "coach_yitong", display_name: str = "怡彤老师"):
    token = sign_admin_token(username, role, display_name, admin_auth_secret())
    return SimpleNamespace(headers={"authorization": f"Bearer {token}"}, state=SimpleNamespace())


class CoachDataScopeTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.coach_request = scoped_request()
        self.admin_request = scoped_request("admin", "admin", "系统管理员")
        self.db.add_all(
            [
                Family(family_id="own", parent_nickname="林妈妈", coach_name="怡彤老师"),
                Family(family_id="other", parent_nickname="周爸爸", coach_name="其他老师"),
                ParentProfile(family_id="own", trust_level="A"),
                ParentProfile(family_id="other", trust_level="C"),
                WeeklyReport(family_id="own", status="draft", final_text="自己的周报"),
                WeeklyReport(family_id="other", status="draft", final_text="其他老师周报"),
                AIOutput(family_id="own", agent_type="ai_reply", display_text="自己的 AI"),
                AIOutput(family_id="other", agent_type="ai_reply", display_text="其他老师 AI"),
                SendTask(family_id="own", target_name="林妈妈", scene="回复", content="自己的任务", status="pending"),
                SendTask(family_id="other", target_name="周爸爸", scene="回复", content="其他老师任务", status="pending"),
                SendLog(task_id=1, family_id="own", target_name="林妈妈", status="sent", detail="自己的日志"),
                SendLog(task_id=2, family_id="other", target_name="周爸爸", status="sent", detail="其他老师日志"),
            ]
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def ids(self, rows):
        return [item["family_id"] for item in rows]

    def test_coach_list_endpoints_only_return_own_families(self):
        self.assertEqual(self.ids(list_families(request=self.coach_request, db=self.db)), ["own"])
        self.assertEqual(self.ids(list_reports(request=self.coach_request, db=self.db)), ["own"])
        self.assertEqual(self.ids(list_profiles(request=self.coach_request, db=self.db)), ["own"])
        self.assertEqual(self.ids(list_ai_outputs(request=self.coach_request, db=self.db)), ["own"])
        self.assertEqual(self.ids(list_send_tasks(request=self.coach_request, db=self.db)), ["own"])
        self.assertEqual(self.ids(list_send_logs(request=self.coach_request, db=self.db)), ["own"])
        self.assertEqual(set(self.ids(list_families(request=self.admin_request, db=self.db))), {"own", "other"})

    def test_coach_cannot_open_or_create_for_other_coach_family(self):
        with self.assertRaises(HTTPException) as blocked_detail:
            family_detail("other", request=self.coach_request, db=self.db)
        self.assertEqual(blocked_detail.exception.status_code, 403)

        with self.assertRaises(HTTPException) as blocked_create:
            create_send_task(
                SendTaskIn(family_id="other", target_name="周爸爸", scene="回复", content="越权任务"),
                request=self.coach_request,
                db=self.db,
            )
        self.assertEqual(blocked_create.exception.status_code, 403)

    def test_coach_upsert_family_is_forced_to_own_scope(self):
        created = upsert_family(
            FamilyIn(family_id="new", parent_nickname="陈妈妈", coach_name=""),
            request=self.coach_request,
            db=self.db,
        )
        self.assertEqual(created["coach_name"], "怡彤老师")

        with self.assertRaises(HTTPException) as blocked:
            upsert_family(
                FamilyIn(family_id="bad", parent_nickname="王妈妈", coach_name="其他老师"),
                request=self.coach_request,
                db=self.db,
            )
        self.assertEqual(blocked.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
