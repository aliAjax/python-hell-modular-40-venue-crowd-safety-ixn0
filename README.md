# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等和审计查询。
- `src/service.py`：用例编排、权限校验和版本控制。
- `src/http_api.py`：JSON接口和统一错误响应。
- `src/audit.py`：操作审计。
- `static/index.html`：最小演示页面。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8340
```

## 核心对象

`venue`为场馆，`zone`为区域，`gate`为入场口，`post`为安保岗位，`medical_point`为医疗点，`incident`为事件，`task`为现场任务。

## 接口

- `GET /health`
- `GET /api/<kind>`，可用`?status=`过滤
- `GET /api/entities/<id>`
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/audit`

事件归并（同一现场状况的多个来源合并为一个主事件）：

- `GET /api/incidents/pending`：待归并台账，列出尚未归并的事件来源。
- `POST /api/incidents/merge`：协调员选主事件并归并。请求体`{"main_incident_id": ..., "source_incident_ids": [...]}`，可带`Idempotency-Key`断网重试。归并后按最高严重度重新定级，未执行（草稿）任务转到主事件，已派出任务保留在原来源、不重复生成。
- `POST /api/incident-merges/<id>/cancel`：撤销归并，恢复原事件与任务。仅协调员可撤销；请求体`{"reason": ..., "expected_version": ...}`支持乐观锁版本控制。
- `GET /api/incidents/<id>/detail`：事件详情，显示主事件、来源清单和未决任务数（归并事件统计整个归并的未决任务）。

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
