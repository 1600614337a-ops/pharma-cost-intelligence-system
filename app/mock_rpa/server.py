"""In-memory mock RPA endpoint used by local and container demonstrations."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(
    title="模拟RPA整改任务服务",
    description="竞赛演示环境中的整改任务接收、查询与通知模拟服务",
    version="1.1.0",
)

tasks_db: dict[str, dict] = {}
notifications_db: list[dict] = []


class Assignee(BaseModel):
    name: str
    department: str
    role: Optional[str] = None


class Source(BaseModel):
    analysis_type: str
    analysis_month: str
    product: str
    finding: str


class TaskCreateRequest(BaseModel):
    task_id: str
    task_title: str
    assignee: Assignee
    source: Source
    priority: str
    deadline: str
    suggestion: Optional[str] = None
    notify_method: Optional[str] = "wechat"
    created_at: str


class WechatNotifyRequest(BaseModel):
    recipient: str
    department: str
    message: str


def _auto_advance_status(task_id: str) -> None:
    time.sleep(30)
    if task_id not in tasks_db:
        return
    tasks_db[task_id]["status"] = "received"
    tasks_db[task_id]["status_history"].append(
        {"status": "received", "time": datetime.now().isoformat()}
    )


@app.get("/health")
def health_check() -> dict:
    return {
        "status": "ok",
        "tasks_count": len(tasks_db),
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/api/rpa/tasks")
def create_task(req: TaskCreateRequest) -> dict:
    if req.priority not in ("high", "medium", "low"):
        raise HTTPException(400, "priority必须为 high/medium/low")
    if req.task_id in tasks_db:
        raise HTTPException(400, f"任务ID {req.task_id} 已存在")

    now = datetime.now().isoformat()
    task = {
        "task_id": req.task_id,
        "task_title": req.task_title,
        "assignee": req.assignee.model_dump(),
        "source": req.source.model_dump(),
        "priority": req.priority,
        "deadline": req.deadline,
        "suggestion": req.suggestion,
        "notify_method": req.notify_method,
        "created_at": req.created_at,
        "status": "sent",
        "status_history": [{"status": "sent", "time": now}],
        "progress": "",
    }
    tasks_db[req.task_id] = task

    notify_result = None
    if req.notify_method == "wechat":
        notify_result = {
            "wechat": f"已发送至 {req.assignee.name}({req.assignee.department})",
            "sent_at": now,
        }
        notifications_db.append(
            {
                "message_id": f"WX-{datetime.now().strftime('%Y%m%d')}-{len(notifications_db) + 1:03d}",
                "recipient": req.assignee.name,
                "task_id": req.task_id,
                "sent_at": now,
            }
        )

    threading.Thread(target=_auto_advance_status, args=(req.task_id,), daemon=True).start()
    return {
        "code": 200,
        "message": "任务创建成功，已分发至责任人",
        "data": {
            "task_id": req.task_id,
            "status": "sent",
            "notify_status": notify_result,
            "tracking_url": f"http://localhost:8090/api/rpa/tasks/{req.task_id}",
        },
    }


@app.get("/api/rpa/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    if task_id not in tasks_db:
        raise HTTPException(404, f"任务 {task_id} 不存在")
    return {"code": 200, "message": "查询成功", "data": tasks_db[task_id]}


@app.get("/api/rpa/tasks")
def list_tasks(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    product: Optional[str] = None,
    month: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    tasks = list(tasks_db.values())
    if status:
        tasks = [task for task in tasks if task["status"] == status]
    if priority:
        tasks = [task for task in tasks if task["priority"] == priority]
    if product:
        tasks = [task for task in tasks if task["source"]["product"] == product]
    if month:
        tasks = [task for task in tasks if task["source"]["analysis_month"] == month]
    tasks.sort(key=lambda task: task["created_at"], reverse=True)
    total = len(tasks)
    start = (page - 1) * page_size
    selected = tasks[start : start + page_size]
    return {
        "code": 200,
        "message": "查询成功",
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "tasks": [
                {
                    "task_id": task["task_id"],
                    "task_title": task["task_title"],
                    "priority": task["priority"],
                    "status": task["status"],
                    "deadline": task["deadline"],
                    "assignee": f"{task['assignee']['name']}({task['assignee']['department']})",
                    "created_at": task["created_at"],
                }
                for task in selected
            ],
        },
    }


@app.post("/api/notify/wechat")
def send_wechat(req: WechatNotifyRequest) -> dict:
    now = datetime.now().isoformat()
    message_id = f"WX-{datetime.now().strftime('%Y%m%d')}-{len(notifications_db) + 1:03d}"
    notifications_db.append(
        {
            "message_id": message_id,
            "recipient": req.recipient,
            "department": req.department,
            "message": req.message,
            "sent_at": now,
        }
    )
    return {
        "code": 200,
        "message": "微信消息发送成功",
        "data": {
            "recipient": req.recipient,
            "message_id": message_id,
            "sent_at": now,
            "status": "delivered",
        },
    }


@app.get("/api/stats")
def get_stats() -> dict:
    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for task in tasks_db.values():
        by_status[task["status"]] = by_status.get(task["status"], 0) + 1
        by_priority[task["priority"]] = by_priority.get(task["priority"], 0) + 1
    return {
        "code": 200,
        "data": {
            "total_tasks": len(tasks_db),
            "total_notifications": len(notifications_db),
            "by_status": by_status,
            "by_priority": by_priority,
        },
    }


@app.delete("/api/admin/reset")
def reset_all() -> dict:
    tasks_db.clear()
    notifications_db.clear()
    return {"code": 200, "message": "所有数据已重置"}
