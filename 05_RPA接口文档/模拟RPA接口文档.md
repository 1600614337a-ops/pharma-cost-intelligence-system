# 模拟RPA整改任务接口文档

> 版本: V1.1
> 更新日期: 2026-08-29

---

## 一、接口概述

模拟RPA服务用于接收成本智能分析系统自动生成的整改任务，模拟企业内部RPA系统的任务分发功能。参赛团队的系统通过HTTP POST将整改任务JSON发送到模拟RPA接口，模拟服务返回执行状态。

### 1.1 基础信息

| 项目 | 内容 |
|------|------|
| 接口地址 | `http://localhost:8090/api/rpa/tasks` |
| 请求方式 | POST |
| Content-Type | application/json |
| 认证方式 | 无需认证（模拟环境） |
| 超时设置 | 30秒 |

### 1.2 接口基础URL

```
开发环境: http://localhost:8090
API前缀:   /api/rpa
```

---

## 二、接口列表

### 2.1 创建整改任务（核心接口）

**接口路径**: `POST /api/rpa/tasks`

**功能**: 接收成本分析系统生成的整改任务，模拟RPA任务分发。

**请求体 (JSON)**:

```json
{
  "task_id": "TASK-2026-0001",
  "task_title": "请核查A药材2026年5月采购合同调价条款",
  "assignee": {
    "name": "张伟",
    "department": "采购部",
    "role": "采购经理"
  },
  "source": {
    "analysis_type": "月度成本分析",
    "analysis_month": "2026-05",
    "product": "银黄口服液",
    "finding": "A药材(金银花)采购价环比上涨12%，超出波动阈值10%"
  },
  "priority": "high",
  "deadline": "2026-07-01",
  "suggestion": "1. 核查2026年采购合同中的调价条款\\n2. 比对新老供应商报价\\n3. 评估是否需要启动询比价",
  "notify_method": "wechat",
  "created_at": "2026-06-15T10:30:00"
}
```

**字段说明**:

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task_id | string | 是 | 任务唯一编号，建议格式: TASK-YYYY-NNNN |
| task_title | string | 是 | 任务标题，简洁描述任务内容 |
| assignee.name | string | 是 | 责任人姓名 |
| assignee.department | string | 是 | 责任部门 |
| assignee.role | string | 否 | 责任人岗位 |
| source.analysis_type | string | 是 | 分析类型: 月度成本分析/季度成本分析/专题分析 |
| source.analysis_month | string | 是 | 分析月份，格式: YYYY-MM |
| source.product | string | 是 | 关联产品名称 |
| source.finding | string | 是 | 分析发现的问题描述 |
| priority | string | 是 | 优先级: high/medium/low |
| deadline | string | 是 | 截止日期，格式: YYYY-MM-DD |
| suggestion | string | 否 | 改进建议（AI生成） |
| notify_method | string | 否 | 通知方式: wechat/email/sms |
| created_at | string | 是 | 任务创建时间，ISO 8601格式 |

**成功响应 (200)**:

```json
{
  "code": 200,
  "message": "任务创建成功，已分发至责任人",
  "data": {
    "task_id": "TASK-2026-0001",
    "status": "sent",
    "notify_status": {
      "wechat": "已发送至 张伟(采购部)",
      "sent_at": "2026-06-15T10:30:01"
    },
    "tracking_url": "http://localhost:8090/api/rpa/tasks/TASK-2026-0001"
  }
}
```

**失败响应 (400/500)**:

```json
{
  "code": 400,
  "message": "请求参数校验失败: task_title不能为空",
  "data": null
}
```

### 2.2 查询任务状态

**接口路径**: `GET /api/rpa/tasks/{task_id}`

**功能**: 查询指定整改任务的执行状态。

**路径参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| task_id | string | 任务编号 |

**成功响应 (200)**:

```json
{
  "code": 200,
  "message": "查询成功",
  "data": {
    "task_id": "TASK-2026-0001",
    "task_title": "请核查A药材2026年5月采购合同调价条款",
    "status": "confirmed",
    "status_history": [
      {"status": "sent", "time": "2026-06-15T10:30:01"},
      {"status": "received", "time": "2026-06-15T10:35:00"},
      {"status": "confirmed", "time": "2026-06-15T11:00:00"}
    ],
    "assignee": {
      "name": "张伟",
      "department": "采购部"
    },
    "deadline": "2026-07-01",
    "progress": "正在比对新老供应商报价"
  }
}
```

**任务状态说明**:

| 状态 | 说明 |
|------|------|
| sent | 已发送至责任人 |
| received | 责任人已接收 |
| confirmed | 责任人已确认开始处理 |
| in_progress | 处理中 |
| completed | 已完成 |
| overdue | 已逾期 |

### 2.3 查询任务列表

**接口路径**: `GET /api/rpa/tasks`

**功能**: 查询所有整改任务列表。

**请求参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| status | string | 否 | 按状态筛选: sent/received/confirmed/in_progress/completed/overdue |
| priority | string | 否 | 按优先级筛选: high/medium/low |
| product | string | 否 | 按产品筛选 |
| month | string | 否 | 按分析月份筛选: YYYY-MM |
| page | int | 否 | 页码，默认1 |
| page_size | int | 否 | 每页条数，默认20 |

**成功响应 (200)**:

```json
{
  "code": 200,
  "message": "查询成功",
  "data": {
    "total": 12,
    "page": 1,
    "page_size": 20,
    "tasks": [
      {
        "task_id": "TASK-2026-0001",
        "task_title": "请核查A药材2026年5月采购合同调价条款",
        "priority": "high",
        "status": "in_progress",
        "deadline": "2026-07-01",
        "assignee": "张伟(采购部)"
      }
    ]
  }
}
```

---

## 三、模拟微信消息推送接口

### 3.1 发送微信消息

**接口路径**: `POST /api/notify/wechat`

**功能**: 模拟通过企业微信/个人微信向指定责任人发送整改任务通知。

**请求体 (JSON)**:

```json
{
  "recipient": "张伟",
  "department": "采购部",
  "message": "【成本整改任务】\n任务编号: TASK-2026-0001\n任务: 请核查A药材2026年5月采购合同调价条款\n优先级: 高\n来源: 2026年5月成本分析-银黄口服液\n截止: 2026-07-01\n请及时处理!"
}
```

**成功响应 (200)**:

```json
{
  "code": 200,
  "message": "微信消息发送成功",
  "data": {
    "recipient": "张伟",
    "message_id": "WX-20260615-001",
    "sent_at": "2026-06-15T10:30:01",
    "status": "delivered"
  }
}
```

---

## 四、模拟RPA服务启动方式

### 4.1 Docker启动（推荐）

```bash
cd 模拟数据/05_RPA接口文档/
docker build -t mock-rpa-service .
docker run -d -p 8090:8090 --name mock-rpa mock-rpa-service
```

### 4.2 Python直接启动

```bash
cd 模拟数据/05_RPA接口文档/
pip install -r requirements.txt
python mock_rpa_server.py
```

服务启动后访问:
- API文档: http://localhost:8090/docs (FastAPI自动生成的Swagger UI)
- 健康检查: http://localhost:8090/health
- 统计信息: http://localhost:8090/api/stats

测试或重复演示前可调用 `DELETE /api/admin/reset` 清空内存中的任务与通知统计。该接口只用于本地演示环境。

---

## 五、测试用例

### 5.1 基本功能测试

```bash
# 创建任务
curl -X POST http://localhost:8090/api/rpa/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "TASK-2026-0001",
    "task_title": "请核查金银花2026年5月采购合同调价条款",
    "assignee": {"name": "张伟", "department": "采购部", "role": "采购经理"},
    "source": {
      "analysis_type": "月度成本分析",
      "analysis_month": "2026-05",
      "product": "银黄口服液",
      "finding": "金银花采购价环比上涨12%，超出波动阈值10%"
    },
    "priority": "high",
    "deadline": "2026-07-01",
    "suggestion": "核查采购合同中调价条款，比对新老供应商报价",
    "notify_method": "wechat",
    "created_at": "2026-06-15T10:30:00"
  }'

# 查询任务
curl http://localhost:8090/api/rpa/tasks/TASK-2026-0001

# 查询列表
curl "http://localhost:8090/api/rpa/tasks?status=sent&priority=high"
```

### 5.2 批量任务测试

参赛系统应至少验证3个整改任务的创建和查询流程，对应考题评测要求中的3个分析场景。

---

## 六、参赛团队集成指南

### 6.1 Python调用示例

```python
import requests

def send_rpa_task(analysis_result):
    """将分析结论转化为RPA整改任务"""
    url = "http://localhost:8090/api/rpa/tasks"
    
    payload = {
        "task_id": f"TASK-{analysis_result['month']}-{analysis_result['seq']:04d}",
        "task_title": analysis_result['suggestion_title'],
        "assignee": {
            "name": analysis_result['assignee'],
            "department": analysis_result['department'],
            "role": analysis_result['role']
        },
        "source": {
            "analysis_type": analysis_result['analysis_type'],
            "analysis_month": analysis_result['month'],
            "product": analysis_result['product'],
            "finding": analysis_result['finding']
        },
        "priority": analysis_result['priority'],
        "deadline": analysis_result['deadline'],
        "suggestion": analysis_result['suggestion'],
        "notify_method": "wechat",
        "created_at": analysis_result['created_at']
    }
    
    resp = requests.post(url, json=payload, timeout=30)
    return resp.json()

# 调用示例
result = send_rpa_task({
    "month": "2026-05",
    "seq": 1,
    "suggestion_title": "请核查金银花2026年5月采购合同调价条款",
    "assignee": "张伟",
    "department": "采购部",
    "role": "采购经理",
    "analysis_type": "月度成本分析",
    "product": "银黄口服液",
    "finding": "金银花采购价环比上涨12%",
    "priority": "high",
    "deadline": "2026-07-01",
    "suggestion": "核查调价条款，比对新老供应商报价",
    "created_at": "2026-06-15T10:30:00"
})
print(result)
```

### 6.2 前端状态展示

建议在系统前端展示以下信息：
- 已生成任务数
- 已发送数
- 已确认数
- 各优先级任务分布

---

## 七、模拟服务返回的任务状态说明

| 模拟场景 | task_id特征 | 创建后自动状态 |
|----------|-----------|-------------|
| 正常分发 | 不含特殊后缀 | sent →（30秒后）received |
| 快速确认 | 含 `-FAST` | sent →（30秒后）received →（再过10秒）confirmed |
| 超期未处理 | 含 `-OVERDUE` | sent，保持不自动推进 |
| 已完成 | 含 `-DONE` | sent → received → confirmed → in_progress → completed |

> 注：状态转换由模拟服务自动推进，参赛团队通过 `GET /api/rpa/tasks/{task_id}` 可观测状态变化。

---

> 文档版本: V1.1 | 编制: 龚云 | 日期: 2026-08-29 | 公司: 重庆创灵境数字技术有限公司
