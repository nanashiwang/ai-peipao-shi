"""场景与风险规则。

这里存放的是纯规则判断，不依赖数据库和外部接口，方便被导入到多个业务流程中。
"""

CHECKIN_RULES = {
    "完成打卡": ["已完成", "打卡", "完成了", "已提交"],
    "未完成": ["没完成", "有事", "来不及", "忘了"],
    "补打卡": ["补打卡", "昨天忘了", "补一下"],
    "请假": ["请假", "上不了", "缺席"],
}

SCENE_RULES = {
    "请假/孩子有事": ["请假", "有事", "上不了", "缺席"],
    "课程时间询问": ["几点上课", "课程时间", "什么时候上课", "几点开始"],
    "资料/链接领取": ["资料", "链接", "怎么领取", "发一下"],
    "打卡规则询问": ["怎么打卡", "打卡规则", "在哪里打卡"],
    "补打卡说明": ["补打卡", "昨天忘了", "补一下", "来不及", "没时间", "作业多"],
    "简单学情询问": ["最近怎么样", "学得怎么样", "孩子状态", "反馈一下"],
}

RISK_WORDS = ["退费", "投诉", "不满意", "没效果", "情绪崩", "吵架"]
PAIN_POINT_WORDS = {
    "作业拖延": ["拖延", "磨蹭", "作业慢"],
    "手机": ["手机", "游戏", "短视频"],
    "成绩波动": ["成绩", "考试", "下滑", "波动"],
    "亲子冲突": ["吵架", "顶嘴", "冲突"],
    "执行力弱": ["坚持不了", "忘了", "没完成", "执行"],
}


# 根据关键词判断当前内容是否属于某种打卡状态。
def detect_checkin(content: str) -> str:
    text = content or ""
    for status, words in CHECKIN_RULES.items():
        if any(word in text for word in words):
            return status
    return ""


# 根据家长输入判断它更像哪类咨询场景。
def detect_scene(content: str) -> str:
    text = content or ""
    if any(word in text for word in RISK_WORDS):
        return "转人工"
    for scene, words in SCENE_RULES.items():
        if any(word in text for word in words):
            return scene
    return ""


# 从一组消息里提炼常见痛点标签，给画像和周报做结构化输入。
def detect_pain_points(contents: list[str]) -> list[str]:
    joined = "\n".join(contents)
    points = [name for name, words in PAIN_POINT_WORDS.items() if any(word in joined for word in words)]
    return points or ["执行稳定性待观察"]
