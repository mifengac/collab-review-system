"""五类 OA_WORK_MODULES 请求参数精确断言（不连接真实 OA）。"""
from __future__ import annotations

from app.services.oa_client import (
    OA_WORK_MODULES,
    module_form_template,
    module_query_params,
)

EXPECTED = {
    "todo": {
        "service": "flowDealingList",
        "query": {
            "noReportLog": "1",
            "service": "flowDealingList",
            "taskType": "0",
        },
        "form_keys": {"page", "flowInstName", "showOnlyMe", "orderOption"},
    },
    "unread": {
        "service": "flowUnreadList",
        "query": {
            "noReportLog": "1",
            "service": "flowUnreadList",
            "taskType": "3",
            "readFlag": "0",
        },
        "form_keys": {"page", "flowInstName", "showOnlyMe", "orderOption"},
    },
    "done": {
        "service": "flowDealingList",
        "query": {
            "noReportLog": "1",
            "service": "flowDealingList",
            "taskType": "1",
        },
        "form_keys": {"page", "showOnlyMe", "orderOption"},
    },
    "read_done": {
        "service": "flowUnreadList",
        "query": {
            "noReportLog": "1",
            "service": "flowUnreadList",
            "taskType": "3",
            "readFlag": "1",
        },
        "form_keys": {"page", "flowInstName", "showOnlyMe", "orderOption"},
    },
    "running": {
        "service": "flowDealingList",
        "query": {
            "noReportLog": "1",
            "service": "flowDealingList",
            "taskType": "-1",
            "readFlag": "0",
        },
        "form_keys": {"page", "showOnlyMe", "orderOption"},
    },
}


def test_all_five_modules_defined():
    assert set(OA_WORK_MODULES.keys()) == set(EXPECTED.keys())


def test_todo_requires_task_type_0():
    q = module_query_params("todo")
    assert q["taskType"] == "0"
    assert q["service"] == "flowDealingList"
    assert "readFlag" not in q


def test_unread_requires_task_type_3_and_read_flag_0():
    q = module_query_params("unread")
    assert q["taskType"] == "3"
    assert q["readFlag"] == "0"
    assert q["service"] == "flowUnreadList"


def test_each_module_query_and_form_exact():
    for code, exp in EXPECTED.items():
        cfg = OA_WORK_MODULES[code]
        assert cfg["service"] == exp["service"]
        assert cfg["query"] == exp["query"]
        assert module_query_params(code) == exp["query"]
        form = module_form_template(code)
        assert set(form.keys()) == exp["form_keys"]
        assert form["page"] == "{page}"
        assert form.get("orderOption") == "1"
        assert form.get("showOnlyMe") == "false"
        # 不向 /hmoa/s 强行传 displayRow
        assert "displayRow" not in form
        assert "displayRow" not in cfg["query"]


def test_no_display_row_in_any_module():
    for code, cfg in OA_WORK_MODULES.items():
        blob = str(cfg)
        assert "displayRow" not in blob, code
