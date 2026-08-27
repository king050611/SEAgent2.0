#!/usr/bin/env python3
"""
TaskMonitor 启动入口
完全离线，复用原项目 LLM 加载逻辑
增加后台轮询扫描 task 文件夹
"""

import os
import sys
from pathlib import Path
import threading
import time
import subprocess
import signal
import socket

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent))

import torch
from vllm import LLM
from transformers import AutoTokenizer

from src.server import create_app
from src.llm_client import LLMClient
from src.state_store import StateStore
from src.task_manager import TaskManager
from src.criteria_evaluator import CriteriaEvaluator
from src.task_decomposer import TaskDecomposer
from src.anomaly_handler import AnomalyHandler
# ############# anomaly advisor 接入开始 #############
from src.anomaly_advisor import AnomalyAdvisor
# ############# anomaly advisor 接入结束 #############
from src.intervention_handler import InterventionHandler
from src.query_responder import QueryResponder
from src.state_monitor import StateMonitor
from src.task_scanner import TaskScanner
from src.utils import load_yaml_config, resolve_path_from_base

# 强制离线环境
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"

# 加载配置
BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
MONITOR_CONFIG = load_yaml_config(CONFIG_DIR / "monitor.yaml")
TASK_TEMPLATES = load_yaml_config(CONFIG_DIR / "task_templates.yaml")
CRITERIA_CONFIG = load_yaml_config(CONFIG_DIR / "criteria.yaml")
STATE_MAPPING = load_yaml_config(CONFIG_DIR / "state_mapping.yaml")

LOCAL_MODEL_PATH = str(resolve_path_from_base(MONITOR_CONFIG["llm"]["model_path"], BASE_DIR))
PORT = MONITOR_CONFIG["server"]["port"]


# ========== 增强的端口清理（使用 psutil） ==========
def find_process_using_port(port):
    """使用 psutil 查找占用指定端口的进程，返回进程对象列表"""
    try:
        import psutil
        processes = []
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                try:
                    proc = psutil.Process(conn.pid)
                    processes.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        return processes
    except ImportError:
        return []


def kill_process_on_port(port):
    """使用 psutil 强制终止占用端口的进程；若 psutil 不可用则尝试系统命令（不报错）"""
    try:
        import psutil
        procs = find_process_using_port(port)
        if not procs:
            print(f"ℹ️ 没有进程占用端口 {port}")
            return True
        for p in procs:
            print(f"🔪 终止进程 PID={p.pid} (名称: {p.name()})")
            p.kill()
        time.sleep(0.5)
        # 检查是否释放
        return not find_process_using_port(port)
    except (ImportError, AttributeError):
        # psutil 不可用，回退到系统命令（不再因命令缺失而报错）
        print("⚠️ psutil 不可用，尝试使用系统命令清理...")
        try:
            import subprocess
            for cmd in [
                f"fuser -k {port}/tcp 2>/dev/null",
                f"lsof -t -i:{port} | xargs -r kill -9 2>/dev/null",
                f"netstat -tlnp 2>/dev/null | grep :{port} | awk '{{print $7}}' | cut -d'/' -f1 | xargs -r kill -9",
                f"ss -tlnp 2>/dev/null | grep :{port} | awk '{{print $6}}' | cut -d'=' -f2 | cut -d',' -f1 | xargs -r kill -9",
            ]:
                subprocess.run(cmd, shell=True, check=False, timeout=2)
            time.sleep(1)
            # 再次用 socket 检测
            return check_port_free(port)
        except Exception:
            return False


def check_port_free(port):
    """检查端口是否可绑定（未被占用）"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('0.0.0.0', port))
        sock.close()
        return True
    except OSError:
        return False
    finally:
        sock.close()


def force_free_port(port, max_retries=3):
    """
    强制释放端口，重试多次。
    如果最终无法释放，打印详细提示并抛出异常。
    """
    for attempt in range(max_retries):
        if kill_process_on_port(port):
            print(f"✅ 端口 {port} 已释放（尝试 {attempt+1}）")
            return
        print(f"⚠️ 端口 {port} 仍被占用，重试 {attempt+1}/{max_retries}...")
        time.sleep(1.5)

    # 若最终失败，打印占用信息并提示手动清理
    try:
        import subprocess
        result = subprocess.run(
            f"lsof -i:{port} || ss -tlnp | grep {port} || netstat -tlnp | grep {port}",
            shell=True, capture_output=True, text=True, timeout=3
        )
        print(f"❌ 端口 {port} 始终被占用，当前占用信息：\n{result.stdout}")
    except Exception:
        pass
    raise RuntimeError(
        f"端口 {port} 无法释放，请手动检查并终止占用进程。\n"
        f"可使用命令：sudo kill -9 $(sudo lsof -t -i:{port})  或  sudo fuser -k {port}/tcp"
    )


def kill_vllm_processes():
    """终止所有包含 'vllm' 的 Python 进程（谨慎使用）"""
    try:
        # 优先使用 pgrep
        result = subprocess.run(
            ["pgrep", "-f", "vllm"],
            capture_output=True,
            text=True,
            check=False
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split()
            for p in pids:
                try:
                    os.kill(int(p), signal.SIGKILL)
                    print(f"✅ Killed vLLM process {p}")
                except ProcessLookupError:
                    pass
            time.sleep(0.5)
    except FileNotFoundError:
        # 如果没有 pgrep，使用 ps + grep
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                check=False
            )
            for line in result.stdout.split('\n'):
                if "vllm" in line and "python" in line:
                    parts = line.split()
                    if parts:
                        pid = parts[1]
                        try:
                            os.kill(int(pid), signal.SIGKILL)
                            print(f"✅ Killed vLLM process {pid} (via ps)")
                        except (ProcessLookupError, ValueError):
                            pass
            time.sleep(0.5)
        except FileNotFoundError:
            pass


def cleanup_before_start():
    """启动前清理所有可能导致冲突的资源"""
    print("🧹 Cleaning up resources...")
    # 先结束 vLLM 相关进程（可能会占用端口或 GPU）
    kill_vllm_processes()
    # 然后强制释放指定端口
    force_free_port(PORT)
    # 额外清理 EngineCore 进程（原遗留）
    os.system("pkill -f VLLM::EngineCore 2>/dev/null")
    # 等待进程完全退出
    time.sleep(1)
    print("✅ Cleanup done.")


# ========== 原有启动逻辑 ==========
def background_scanner(scanner: TaskScanner, interval: int = 60):
    """后台线程轮询扫描 task 文件夹，默认间隔60秒"""
    while True:
        time.sleep(interval)
        scanner.scan_and_create()


def startup():
    """初始化所有组件"""
    # ----- 启动前清理 -----
    cleanup_before_start()

    print("🚀 TaskMonitor 启动中...")

    # 1. 加载 LLM
    print("Loading tokenizer...")
    tok = AutoTokenizer.from_pretrained(
        LOCAL_MODEL_PATH,
        trust_remote_code=True,
        local_files_only=True,
    )
    print("Loading vLLM model...")
    llm_engine = LLM(
        model=LOCAL_MODEL_PATH,
        trust_remote_code=True,
        dtype="bfloat16" if torch.cuda.is_bf16_supported() else "float16",
        max_num_seqs=1,
    )
    llm_client = LLMClient(llm_engine, tok)

    # 2. 持久化存储（任务状态存放在 data/tasks/ 下）
    storage_dir = MONITOR_CONFIG["persistence"].get("directory", "data/tasks")
    state_store = StateStore(storage_dir)

    # 3. 核心组件
    task_decomposer = TaskDecomposer(TASK_TEMPLATES)
    criteria_evaluator = CriteriaEvaluator(CRITERIA_CONFIG, STATE_MAPPING)
    anomaly_handler = AnomalyHandler(TASK_TEMPLATES, state_store)
    # ############# anomaly advisor 接入开始 #############
    anomaly_advisor = AnomalyAdvisor(llm_client=llm_client)
    # ############# anomaly advisor 接入结束 #############
    intervention_handler = InterventionHandler(llm_client, task_decomposer, state_store, criteria_evaluator)
    query_responder = QueryResponder(llm_client)
    state_monitor = StateMonitor(state_store, criteria_evaluator, STATE_MAPPING, query_responder)

    task_manager = TaskManager(
        state_store=state_store,
        task_decomposer=task_decomposer,
        criteria_evaluator=criteria_evaluator,
        anomaly_handler=anomaly_handler,
        intervention_handler=intervention_handler,
        state_monitor=state_monitor,
        # ############# anomaly advisor 接入开始 #############
        anomaly_advisor=anomaly_advisor,
        # ############# anomaly advisor 接入结束 #############
    )

    # 4. 任务扫描器（扫描 task 文件夹）
    task_folder = BASE_DIR / "task"
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    record_file = data_dir / "processed_records.json"

    task_scanner = TaskScanner(
        task_folder=str(task_folder),
        task_manager=task_manager,
        state_store=state_store,
        record_file=str(record_file)
    )

    # 5. 启动时扫描一次
    print("🔍 扫描 task 文件夹中的任务文件...")
    scan_results = task_scanner.scan_and_create()
    for res in scan_results:
        print(f"  - {res['file']}: {res['status']}")

    # 6. 启动后台轮询扫描线程
    scanner_thread = threading.Thread(target=background_scanner, args=(task_scanner, 60), daemon=True)
    scanner_thread.start()
    print("🔄 后台轮询扫描已启动（每60秒）")

    # 7. 创建 Flask App
    app = create_app(
        task_manager=task_manager,
        query_responder=query_responder,
        state_monitor=state_monitor,
        state_store=state_store,
        task_scanner=task_scanner,
    )
    return app


if __name__ == "__main__":
    app = startup()
    print(f"🌐 TaskMonitor running at http://localhost:{PORT}")
    app.run(host=MONITOR_CONFIG["server"]["host"], port=PORT, debug=False, threaded=True)