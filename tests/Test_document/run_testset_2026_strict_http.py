#!/usr/bin/env python3
"""
Strict HTTP runner for tests/Test_document/测试集2026.md.

It uses the real Flask HTTP API and the real QueryResponder behind run.py. The
script records backend inputs/responses and frontend chat inputs/responses for
each manual test case.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "tests" / "Test_document"
TASK_ID = "TI2026021192"
PORT = 8889
BASE_URL = f"http://127.0.0.1:{PORT}"
CONDA_SH = "/root/miniconda3/etc/profile.d/conda.sh"
SEAGENT_PYTHON = "/root/miniconda3/envs/seagent/bin/python"

REPORT_PATH = OUT_DIR / "测试集2026_严格HTTP测试报告.md"
JSON_PATH = OUT_DIR / "测试集2026_严格HTTP测试结果.json"
SUMMARY_PATH = OUT_DIR / "当前版本问题总结_严格HTTP.md"
SERVER_LOG_PATH = OUT_DIR / "测试集2026_严格HTTP_runpy.log"

NORMAL_ANOMALY = {
    "data_commun": "normal",
    "perception": "normal",
    "planning": "normal",
    "execution": "normal",
    "plant": "normal",
}

STATUS_FIXTURES = {
    "S1": {
        "distance_error_m": 0.05,
        "angle_error_deg": 5.0,
        "speed_stable_frames": 5,
        "grid_count": 15,
        "panel_visible_flag": 1,
        "plug_stable_flag": 1,
    },
    "S2": {
        "slot_pose_delta_m": 0.005,
        "plug_pose_delta_m": 0.006,
        "slot_stable_flag": 1,
        "plug_stable_flag": 1,
    },
    "S3": {"ik_valid_flag": 1},
    "S4": {"grasp_done_flag": 1},
    "S5": {"ik_valid_flag": 1},
    "S6": {"insert_done_flag": 1},
    "S7": {"visual_check_flag": 1},
    "S8": {"arm_reset_flag": 1, "return_position_error_m": 0.03},
}


@dataclass
class StepTrace:
    channel: str
    label: str
    request: Any = None
    response_code: int | None = None
    response: Any = None


@dataclass
class Case:
    id: str
    title: str
    section: str
    status: str = "pass"
    checks: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    trace: list[StepTrace] = field(default_factory=list)

    def check(self, ok: bool, message: str, detail: Any = None):
        if ok:
            self.checks.append(message)
            return
        self.status = "fail"
        self.issues.append(format_issue(message, detail))

    def add(self, channel: str, label: str, request: Any, code: int | None, response: Any):
        self.trace.append(StepTrace(channel, label, request, code, response))


class FixtureSnapshot:
    def __init__(self):
        self.backup = OUT_DIR / ".strict_http_fixture_backup"
        self.tasks_dir = ROOT / "data" / "tasks"
        self.record_file = ROOT / "data" / "processed_records.json"

    def snapshot(self):
        if self.backup.exists():
            shutil.rmtree(self.backup)
        self.backup.mkdir(parents=True)
        if self.tasks_dir.exists():
            shutil.copytree(self.tasks_dir, self.backup / "tasks")
        else:
            (self.backup / "tasks").mkdir()
        if self.record_file.exists():
            (self.backup / "processed_records.json").write_bytes(self.record_file.read_bytes())
        else:
            (self.backup / "processed_records.missing").write_text("", encoding="utf-8")

    def prepare(self):
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = self.tasks_dir / f"{TASK_ID}.json"
        if task_file.exists():
            task_file.unlink()
        self.record_file.parent.mkdir(parents=True, exist_ok=True)
        self.record_file.write_text(
            json.dumps({"processed_ids": [f"task_intent_{TASK_ID}.json"]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def restore(self):
        if self.tasks_dir.exists():
            shutil.rmtree(self.tasks_dir)
        shutil.copytree(self.backup / "tasks", self.tasks_dir)
        record_backup = self.backup / "processed_records.json"
        if record_backup.exists():
            self.record_file.write_bytes(record_backup.read_bytes())
        elif self.record_file.exists():
            self.record_file.unlink()
        shutil.rmtree(self.backup)


class HttpHarness:
    def __init__(self, start_server: bool, timeout: int):
        self.start_server = start_server
        self.timeout = timeout
        self.proc: subprocess.Popen[str] | None = None
        self.log_handle = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def ensure_service(self):
        if self.alive():
            return
        if not self.start_server:
            raise RuntimeError(f"{BASE_URL} is not responding; rerun with --start-server or start python run.py manually")
        self.log_handle = SERVER_LOG_PATH.open("w", encoding="utf-8")
        cmd = f"source {CONDA_SH} && conda activate seagent && {SEAGENT_PYTHON} run.py"
        self.proc = subprocess.Popen(
            ["bash", "-lc", cmd],
            cwd=ROOT,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"run.py exited with code {self.proc.returncode}; see {SERVER_LOG_PATH}")
            if self.alive():
                return
            time.sleep(2)
        raise RuntimeError(f"run.py did not become ready within {self.timeout}s; see {SERVER_LOG_PATH}")

    def close(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                self.proc.wait(timeout=20)
        if self.log_handle:
            self.log_handle.close()

    def alive(self) -> bool:
        try:
            result = self.request("GET", "/api/tasks", timeout=3)
            return result[0] == 200
        except Exception:
            return False

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 240) -> tuple[int, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, parse_json(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return exc.code, parse_json(raw)

    def reset_create(self, case: Case):
        code, body = self.request("POST", f"/api/task/{TASK_ID}/reset", {})
        case.add("backend", "reset task", {"method": "POST", "path": f"/api/task/{TASK_ID}/reset"}, code, body)
        intent = json.loads((ROOT / "task" / f"task_intent_{TASK_ID}.json").read_text(encoding="utf-8"))
        code, body = self.request("POST", "/api/task/from_intent", intent)
        case.add("backend", "create task from intent", {"method": "POST", "path": "/api/task/from_intent", "json": intent}, code, body)
        case.check(code == 200 and isinstance(body, dict) and body.get("ok") is True, "任务可从意图书创建", {"code": code, "body": body})

    def status_update(self, case: Case, sid: str, details: dict[str, Any], anomaly: dict[str, Any] | None = None) -> tuple[int, Any]:
        payload = {
            "task_id": TASK_ID,
            "type": "status_update",
            "data": {
                "subtask_id": sid,
                "status": "reported",
                "criteria_details": details,
                "anomaly_state": anomaly or copy.deepcopy(NORMAL_ANOMALY),
            },
        }
        code, body = self.request("POST", "/api/task/update", payload)
        case.add("backend", f"status_update {sid}", {"method": "POST", "path": "/api/task/update", "json": payload}, code, body)
        return code, body

    def approve(self, case: Case, sid: str) -> tuple[int, Any]:
        path = f"/api/task/{TASK_ID}/subtask/{sid}/approve"
        code, body = self.request("POST", path, {})
        case.add("backend", f"approve {sid}", {"method": "POST", "path": path}, code, body)
        return code, body

    def query(self, case: Case, message: str) -> tuple[int, Any]:
        payload = {"task_id": TASK_ID, "global_mode": False, "message": message}
        code, body = self.request("POST", "/api/query", payload)
        case.add("frontend", message, {"method": "POST", "path": "/api/query", "json": payload}, code, body)
        return code, body

    def state(self, case: Case | None = None) -> dict[str, Any]:
        code, body = self.request("GET", f"/api/task/{TASK_ID}/status")
        if case:
            case.add("backend", "read task status", {"method": "GET", "path": f"/api/task/{TASK_ID}/status"}, code, body)
        return body if isinstance(body, dict) else {}

    def subtask(self, sid: str) -> dict[str, Any]:
        state = self.state()
        return next((st for st in state.get("subtasks", []) if st.get("subtask_id") == sid), {})


def parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def format_issue(message: str, detail: Any = None) -> str:
    if detail is None:
        return message
    text = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    if len(text) > 900:
        text = text[:900] + "...<truncated>"
    return f"{message}: {text}"


def advance_to(h: HttpHarness, case: Case, target: str):
    for idx in range(1, int(target[1:])):
        sid = f"S{idx}"
        code, body = h.status_update(case, sid, STATUS_FIXTURES[sid])
        case.check(code == 200 and body.get("action") == "waiting_approval", f"前置 {sid} 后端进入 waiting_approval", {"code": code, "body": body})
        code, body = h.approve(case, sid)
        case.check(code == 200, f"前置 {sid} 人工审核通过", {"code": code, "body": body})


def normal_step(sid: str) -> Callable[[HttpHarness, Case], None]:
    def run(h: HttpHarness, case: Case):
        h.reset_create(case)
        advance_to(h, case, sid)
        code, body = h.status_update(case, sid, STATUS_FIXTURES[sid])
        case.check(code == 200 and body.get("action") == "waiting_approval" and body.get("subtask_id") == sid, f"{sid} 后端返回 waiting_approval", {"code": code, "body": body})
        st = h.subtask(sid)
        case.check(st.get("status") == "waiting_approval", f"{sid} 状态栏为 waiting_approval", st)
        code, body = h.query(case, "当前子任务状态怎么样？")
        case.check(code == 200 and body.get("type") == "query", f"{sid} 前端查询返回 QUERY", {"code": code, "body": body})
    return run


def setup_failed_s1(h: HttpHarness, case: Case, anomaly: dict[str, Any] | None = None):
    code, body = h.status_update(case, "S1", dict(STATUS_FIXTURES["S1"], distance_error_m=0.15), anomaly)
    case.check(code == 200 and body.get("action") == "fail_task", "S1 硬失败返回 fail_task", {"code": code, "body": body})


def setup_progressed_to_s3(h: HttpHarness, case: Case):
    h.status_update(case, "S1", STATUS_FIXTURES["S1"])
    h.approve(case, "S1")
    h.status_update(case, "S2", STATUS_FIXTURES["S2"])
    h.approve(case, "S2")
    case.check(h.state().get("current_subtask") == "S3", "前置推进到 S3")


def case_create(h: HttpHarness, case: Case):
    h.reset_create(case)
    code, body = h.request("GET", "/api/tasks")
    case.add("backend", "list tasks", {"method": "GET", "path": "/api/tasks"}, code, body)
    task = next((item for item in body if item.get("task_id") == TASK_ID), None) if isinstance(body, list) else None
    case.check(task is not None, "/api/tasks 能看到新任务", body)
    state = h.state(case)
    case.check(len(state.get("subtasks", [])) == 8 and state.get("current_subtask") == "S1", "任务生成 S1-S8 且当前为 S1", state)


def case_hard_fail(h: HttpHarness, case: Case):
    h.reset_create(case)
    setup_failed_s1(h, case)
    state = h.state(case)
    case.check(state.get("overall_status") == "failed" and h.subtask("S1").get("status") == "failed", "硬失败后状态栏为 failed", state)
    code, body = h.status_update(case, "S2", STATUS_FIXTURES["S2"])
    case.check(code == 409, "failed 后阻断后续后端上报", {"code": code, "body": body})


def case_retry_current(h: HttpHarness, case: Case):
    h.reset_create(case)
    setup_failed_s1(h, case)
    code, body = h.query(case, "重试当前任务")
    case.check(code == 200 and body.get("type") == "intervention_pending" and body.get("pending_action", {}).get("action") == "retry", "重试当前任务生成 retry 二次确认", {"code": code, "body": body})
    code, body = h.query(case, "确认")
    case.check(code == 200 and body.get("type") == "intervention", "确认后执行 retry", {"code": code, "body": body})
    state = h.state(case)
    case.check(state.get("overall_status") == "in_progress" and h.subtask("S1").get("status") == "in_progress", "retry 后恢复执行中", state)


def case_change_parameter(h: HttpHarness, case: Case):
    h.reset_create(case)
    setup_failed_s1(h, case)
    code, body = h.query(case, "将 S1 的 distance_error_max 改为 0.15")
    case.check(code == 200 and body.get("type") == "intervention_pending" and body.get("intent") == "write", "distance_error_max 修改生成 WRITE 二次确认", {"code": code, "body": body})
    if body.get("type") == "intervention_pending":
        code, body = h.query(case, "确认")
        case.check(code == 200 and body.get("type") == "intervention" and body.get("result", {}).get("ok") is True, "确认后执行 change_parameter", {"code": code, "body": body})
        state = h.state(case)
        case.check(state.get("global_parameters", {}).get("distance_error_max") == 0.15, "distance_error_max 写入参数", state.get("global_parameters"))


def case_override(h: HttpHarness, case: Case):
    h.reset_create(case)
    setup_failed_s1(h, case)
    code, body = h.query(case, "人工确认当前距离误差是 0.05 米")
    case.check(code == 200 and body.get("type") == "intervention_pending" and body.get("intent") == "write", "状态覆盖生成 WRITE 二次确认", {"code": code, "body": body})
    if body.get("type") == "intervention_pending":
        code, body = h.query(case, "确认")
        case.check(code == 200 and body.get("type") == "intervention", "确认后执行 override_field", {"code": code, "body": body})
    st = h.subtask("S1")
    case.check(st.get("user_overrides", {}).get("distance_error_max") == 0.05, "覆盖值写入 user_overrides", st)
    case.check(st.get("status") == "waiting_approval", "覆盖后重新评估为 waiting_approval", st)


def case_soft_fail(h: HttpHarness, case: Case):
    h.reset_create(case)
    code, body = h.status_update(case, "S1", dict(STATUS_FIXTURES["S1"], grid_count=9))
    case.check(code == 200 and body.get("message") == "subtask still in progress", "软判据失败保持进行中", {"code": code, "body": body})
    case.check(h.subtask("S1").get("status") == "in_progress", "状态栏仍为 in_progress", h.subtask("S1"))
    code, body = h.status_update(case, "S1", STATUS_FIXTURES["S1"])
    case.check(code == 200 and body.get("action") == "waiting_approval", "重新上报正确数据后进入 waiting_approval", {"code": code, "body": body})


def case_anomaly(h: HttpHarness, case: Case, anomaly: dict[str, Any], expected_key: str | None):
    h.reset_create(case)
    setup_failed_s1(h, case, anomaly)
    state = h.state(case)
    if expected_key:
        case.check(state.get("anomaly_state", {}).get(expected_key) == "abnormal", f"记录 {expected_key}=abnormal", state.get("anomaly_state"))
    else:
        case.check(all(v == "normal" for v in state.get("anomaly_state", {}).values()), "异常状态均为 normal", state.get("anomaly_state"))
    code, body = h.query(case, "当前系统有什么异常？")
    case.check(code == 200 and body.get("type") == "query", "前端异常查询返回 QUERY", {"code": code, "body": body})


def query_case(message: str, expected_type: str = "query", intervention_setup: bool = False) -> Callable[[HttpHarness, Case], None]:
    def run(h: HttpHarness, case: Case):
        h.reset_create(case)
        setup_failed_s1(h, case)
        if intervention_setup:
            h.query(case, "将 S1 的超时时间改成 60 秒")
            h.query(case, "确认")
        code, body = h.query(case, message)
        case.check(code == 200, "前端聊天接口返回 200", {"code": code, "body": body})
        case.check(body.get("type") == expected_type, f"前端路由为 {expected_type}", body)
    return run


def intervention_retry(h: HttpHarness, case: Case):
    h.reset_create(case)
    setup_failed_s1(h, case)
    code, body = h.query(case, "重试 S1")
    case.check(code == 200 and body.get("type") == "intervention_pending" and body.get("pending_action") == {"action": "retry", "subtask_id": "S1"}, "retry 解析并进入二次确认", {"code": code, "body": body})
    code, body = h.query(case, "确认")
    case.check(code == 200 and body.get("type") == "intervention", "retry 确认后执行", {"code": code, "body": body})
    case.check(h.subtask("S1").get("status") == "in_progress", "retry 后 S1 in_progress", h.subtask("S1"))


def intervention_rollback(h: HttpHarness, case: Case):
    h.reset_create(case)
    setup_progressed_to_s3(h, case)
    code, body = h.query(case, "回退到 S2")
    case.check(code == 200 and body.get("type") == "intervention_pending" and body.get("pending_action") == {"action": "rollback", "to_subtask": "S2"}, "rollback 解析并进入二次确认", {"code": code, "body": body})
    code, body = h.query(case, "确认")
    case.check(code == 200 and body.get("type") == "intervention", "rollback 确认后执行", {"code": code, "body": body})
    case.check(h.state().get("current_subtask") == "S2" and h.subtask("S2").get("status") == "in_progress", "rollback 后回到 S2", h.state())


def intervention_force(h: HttpHarness, case: Case):
    h.reset_create(case)
    code, body = h.query(case, "强制完成 S1")
    case.check(code == 200 and body.get("type") == "intervention_pending" and body.get("pending_action") == {"action": "force_complete", "subtask_id": "S1"}, "force_complete 解析并进入二次确认", {"code": code, "body": body})
    code, body = h.query(case, "确认")
    case.check(code == 200 and body.get("type") == "intervention", "force_complete 确认后执行", {"code": code, "body": body})
    case.check(h.state().get("current_subtask") == "S2" and h.subtask("S1").get("status") == "completed", "force_complete 后推进 S2", h.state())


def build_cases() -> list[tuple[Case, Callable[[HttpHarness, Case], None]]]:
    cases: list[tuple[Case, Callable[[HttpHarness, Case], None]]] = [
        (Case("CREATE", "任务创建", "任务创建"), case_create),
    ]
    for sid in [f"S{i}" for i in range(1, 9)]:
        cases.append((Case(f"NORMAL-{sid}", f"{sid} 正常流程推进", "正常流程推进测试"), normal_step(sid)))
    cases.extend([
        (Case("CR-TS1", "硬判据不满足", "判据失败测试"), case_hard_fail),
        (Case("CR-TS1-RETRY", "硬失败解决方案：重试当前任务", "判据失败测试"), case_retry_current),
        (Case("CR-TS1-CHANGE", "硬失败解决方案：修改判据阈值", "判据失败测试"), case_change_parameter),
        (Case("CR-TS1-OVERRIDE", "硬失败解决方案：覆盖外部状态值", "判据失败测试"), case_override),
        (Case("CR-TS2", "软判据不满足后恢复", "判据失败测试"), case_soft_fail),
        (Case("AS1", "感知异常提示", "异常状态测试"), lambda h, c: case_anomaly(h, c, dict(NORMAL_ANOMALY, perception="abnormal"), "perception")),
        (Case("AS2", "全部状态正常", "异常状态测试"), lambda h, c: case_anomaly(h, c, copy.deepcopy(NORMAL_ANOMALY), None)),
        (Case("QA-TS1", "查询任务进度", "查询问答测试"), query_case("当前任务状态如何呢？")),
        (Case("QA-TS2", "查询任务失败原因", "查询问答测试"), query_case("当前任务为什么失败？")),
        (Case("QA-TS3", "查询子任务判据详情", "查询问答测试"), query_case("S1 的判据详情是什么？")),
        (Case("QA-TS4", "查询系统异常相关信息", "查询问答测试"), query_case("当前系统有什么异常？")),
        (Case("QA-TS5", "查询干预生效情况", "查询问答测试"), query_case("刚才的参数修改生效了吗？", intervention_setup=True)),
        (Case("QA-TS6", "查询任务基础信息", "查询问答测试"), query_case("这个任务水深是多少？")),
        (Case("QA-TS7", "与任务无关的问题", "查询问答测试"), query_case("今天天气怎么样？", expected_type="irrelevant")),
        (Case("INT-TS1", "retry 二次确认与执行", "人工干预确认测试"), intervention_retry),
        (Case("INT-TS2", "rollback 二次确认与执行", "人工干预确认测试"), intervention_rollback),
        (Case("INT-TS3", "change_parameter 二次确认与执行", "人工干预确认测试"), case_change_parameter),
        (Case("INT-TS4", "override_field 二次确认与执行", "人工干预确认测试"), case_override),
        (Case("INT-TS5", "force_complete 二次确认与执行", "人工干预确认测试"), intervention_force),
    ])
    return cases


def collect_baseline(command: str) -> dict[str, Any]:
    def git(args: list[str]) -> str:
        try:
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()
        except Exception as exc:
            return f"unavailable: {exc}"

    gpu = subprocess.run(["bash", "-lc", "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null || true"], cwd=ROOT, text=True, capture_output=True)
    return {
        "revision": git(["rev-parse", "HEAD"]),
        "branch_status": git(["status", "--short", "--branch"]),
        "runner_command": command,
        "service_mode": "strict HTTP against real run.py service; no FakeLLM",
        "frontend_input_method": "POST /api/query, same payload used by web chat",
        "backend_input_method": "POST /api/task/update and /api/task/from_intent",
        "gpu_memory_before": gpu.stdout.strip(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
    }


def run_all(h: HttpHarness) -> list[Case]:
    results = []
    for case, fn in build_cases():
        try:
            fn(h, case)
        except Exception as exc:
            case.status = "fail"
            case.issues.append(f"执行异常: {type(exc).__name__}: {exc}")
        results.append(case)
    return results


def write_outputs(baseline: dict[str, Any], cases: list[Case], fixture_restored: bool):
    summary = {
        "total": len(cases),
        "pass": sum(1 for c in cases if c.status == "pass"),
        "fail": sum(1 for c in cases if c.status == "fail"),
    }
    payload = {
        "baseline": baseline,
        "summary": summary,
        "fixture_restored": fixture_restored,
        "cases": [
            {
                "id": c.id,
                "title": c.title,
                "section": c.section,
                "status": c.status,
                "checks": c.checks,
                "issues": c.issues,
                "trace": [t.__dict__ for t in c.trace],
            }
            for c in cases
        ],
    }
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 测试集2026 严格 HTTP 测试报告",
        "",
        "## 基线",
        "",
        f"- revision: `{baseline['revision']}`",
        f"- runner_command: `{baseline['runner_command']}`",
        f"- service_mode: {baseline['service_mode']}",
        f"- frontend_input_method: {baseline['frontend_input_method']}",
        f"- backend_input_method: {baseline['backend_input_method']}",
        f"- gpu_memory_before: `{baseline['gpu_memory_before']}`",
        f"- fixture_restored: `{fixture_restored}`",
        f"- generated_at: {baseline['generated_at']}",
        "",
        "```text",
        baseline["branch_status"],
        "```",
        "",
        "## 汇总",
        "",
        f"- total: {summary['total']}",
        f"- pass: {summary['pass']}",
        f"- fail: {summary['fail']}",
        "",
        "## 逐用例输入输出",
        "",
    ]
    for case in cases:
        lines.extend([
            f"### {case.id} {case.title}",
            "",
            f"- section: {case.section}",
            f"- status: `{case.status}`",
        ])
        if case.issues:
            lines.append("- issues:")
            lines.extend(f"  - {issue}" for issue in case.issues)
        lines.append("- trace:")
        for step in case.trace:
            lines.extend([
                f"  - {step.channel} / {step.label}",
                f"    - input: `{short_json(step.request)}`",
                f"    - response_code: `{step.response_code}`",
                f"    - response: `{short_json(step.response)}`",
            ])
        lines.append("")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    SUMMARY_PATH.write_text(build_summary(summary, cases, baseline, fixture_restored), encoding="utf-8")


def build_summary(summary: dict[str, int], cases: list[Case], baseline: dict[str, Any], fixture_restored: bool) -> str:
    failed = [c for c in cases if c.status == "fail"]
    lines = [
        "# 当前版本问题总结（严格 HTTP）",
        "",
        "这次不是 FakeLLM 仿真，而是严格走真实 HTTP 服务：后端输入走 `/api/task/update` / `/api/task/from_intent`，前端聊天输入走 `/api/query`。每条用例都在 JSON 和报告里保留了前端输入、后端输入、后端响应、前端响应。",
        "",
        f"测试结果：共 {summary['total']} 个用例，{summary['pass']} 个通过，{summary['fail']} 个失败。fixture 恢复结果：`{fixture_restored}`。",
        "",
    ]
    if not failed:
        lines.extend([
            "## 本轮未发现结构化失败",
            "",
            "按 `测试集2026.md` 的结构化断言看，当前系统对这些用例都能正确响应。自然语言是否完全符合示例措辞，建议再人工抽查报告里的前端响应全文。",
            "",
        ])
    else:
        lines.extend(["## 失败项", ""])
        for c in failed:
            lines.append(f"### {c.id} {c.title}")
            lines.append("")
            for issue in c.issues:
                lines.append(f"- {issue}")
            frontend_steps = [t for t in c.trace if t.channel == "frontend"]
            backend_steps = [t for t in c.trace if t.channel == "backend"]
            if backend_steps:
                last = backend_steps[-1]
                lines.append(f"- 最后一条后端输入：`{short_json(last.request)}`")
                lines.append(f"- 最后一条后端响应：`{short_json(last.response)}`")
            if frontend_steps:
                last = frontend_steps[-1]
                lines.append(f"- 最后一条前端输入：`{last.label}`")
                lines.append(f"- 最后一条前端响应：`{short_json(last.response)}`")
            lines.append("")
    lines.extend([
        "## 证据文件",
        "",
        f"- 测试报告：`{REPORT_PATH}`",
        f"- JSON 明细：`{JSON_PATH}`",
        f"- run.py 日志：`{SERVER_LOG_PATH}`",
        f"- revision：`{baseline['revision']}`",
    ])
    return "\n".join(lines) + "\n"


def short_json(value: Any, limit: int = 1200) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "...<truncated>"


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict HTTP test runner for 测试集2026.md")
    parser.add_argument("--start-server", action="store_true", help="Start python run.py if port 8889 is not already running")
    parser.add_argument("--startup-timeout", type=int, default=300)
    parser.add_argument("--no-restore", action="store_true", help="Do not restore data/tasks after execution")
    args = parser.parse_args()

    command = "source /root/miniconda3/etc/profile.d/conda.sh && conda activate seagent && python tests/Test_document/run_testset_2026_strict_http.py --start-server"
    baseline = collect_baseline(command)
    snapshot = FixtureSnapshot()
    harness = HttpHarness(start_server=args.start_server, timeout=args.startup_timeout)
    fixture_restored = False
    cases: list[Case] = []
    try:
        snapshot.snapshot()
        snapshot.prepare()
        harness.ensure_service()
        cases = run_all(harness)
    except Exception as exc:
        setup = Case("SETUP", "测试环境准备或 run.py 启动", "setup", status="fail")
        setup.issues.append(f"执行异常: {type(exc).__name__}: {exc}")
        cases = [setup]
    finally:
        harness.close()
        if args.no_restore:
            fixture_restored = False
        else:
            try:
                snapshot.restore()
                fixture_restored = True
            except Exception as exc:
                cleanup = Case("CLEANUP", "fixture 恢复", "cleanup", status="fail")
                cleanup.issues.append(f"恢复异常: {type(exc).__name__}: {exc}")
                cases.append(cleanup)
    write_outputs(baseline, cases, fixture_restored)
    summary = {"total": len(cases), "pass": sum(1 for c in cases if c.status == "pass"), "fail": sum(1 for c in cases if c.status == "fail")}
    print(f"严格HTTP测试完成: total={summary['total']} pass={summary['pass']} fail={summary['fail']}")
    print(f"Report: {REPORT_PATH}")
    print(f"JSON: {JSON_PATH}")
    print(f"Summary: {SUMMARY_PATH}")
    for c in cases:
        if c.status == "fail":
            print(f"[FAIL] {c.id} {c.title}")
            for issue in c.issues:
                print(f"  - {issue}")
    return 1 if summary["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
