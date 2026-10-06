# 大型场馆人群安全与现场指挥

只使用Python标准库和SQLite的模块化服务，默认端口`8340`。支持场馆区域、入场口、容量、通道、安保岗位、医疗点、事件、限流、开放通道、疏散和医疗任务、人员到位、区域恢复、延迟重复事件和容量冲突，以及夜间值守的**多来源事件归并**（待归并台账、选主归并、最高严重度重新定级、未执行任务转移、乐观锁并发控制、撤销恢复和本场馆协调员权限）。

## 模块结构

- `app.py`：参数解析、依赖组装和服务生命周期。
- `src/domain.py`：角色、领域异常和数据对象。
- `src/rules.py`：容量计算、事件优先级、状态机和团队冲突约束。
- `src/repository.py`：SQLite持久化、乐观锁、幂等、来源登记表、归并事务工作单元和审计查询。
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
- `GET /api/incidents/<id>`：事件详情，含`merge`视图（主事件、来源清单、未决任务数）
- `POST /api/<kind>`
- `POST /api/entities/<id>/actions`
- `GET /api/sources`：待归并来源台账（默认仅`pending`，`?include_merged=true`查看全部，`?venue_id=`过滤）
- `POST /api/incidents/merge`：事件归并
- `POST /api/merge-records/<id>/unmerge`：撤销归并（仅本场馆协调员）
- `GET /api/audit`

身份通过`X-User-Id`和`X-Role`请求头传入。可选`Idempotency-Key`防止重复创建。

## 事件归并

夜间换班时同一现场常由对讲机（`channel=radio`）、视频（`video`）和电话（`phone`）分别上报，来源编号各不相同。流程：

1. **先进台账**：所有事件上报即进入待归并台账（`source_registry`，按`venue_id+source_ref`唯一），事件初始状态为`reported`。
2. **选主归并**：协调员`POST /api/incidents/merge`，指定`primary_incident_id`与`source_incident_ids`，并带主事件的`expected_version`。
3. **重新定级**：主事件按主事件与全部来源的**最高严重度**重算`severity`和`priority_score`；来源事件置为`merged`并记录`merged_into`。
4. **任务处理**：来源上未执行（`draft`）的任务转到主事件；已派出（`assigned`及之后）的任务留在来源事件，**不重复生成**。
5. **撤销恢复**：`POST /api/merge-records/<id>/unmerge`按归并前快照恢复来源事件的状态与数据，任务退回原事件，来源重新回到台账；撤销后可再次归并。

并发与可靠性：

- **乐观锁**：归并校验主事件版本，撤销校验归并记录版本；两名协调员同时提交时后到者收到`409 ConflictError`，其来源不会被吞并，刷新版本后可重试。
- **断网幂等**：来源登记有数据库唯一约束兜底，同来源重试（无论是否带相同`Idempotency-Key`）都不会再次入库或生成任务；归并请求也支持`Idempotency-Key`，重试返回同一条归并记录。
- **权限**：建馆时通过`coordinator_ids`登记本场馆协调员，也可用场馆动作`assign_coordinators`维护；只有**本场馆**协调员能归并和撤销，其他角色或别馆协调员得到`403 PermissionDenied`。

归并请求示例：

```json
POST /api/incidents/merge
{ "primary_incident_id": "inc-radio-17",
  "source_incident_ids": ["inc-video-09", "inc-phone-03"],
  "expected_version": 1 }
```

事件详情中`merge`字段：`role`（`standalone`/`primary`/`merged_source`）、`primary_incident_id`、`sources`（来源清单，含通道与严重度）、`open_task_count`（整个归并组的未决任务数）、`retained_tasks`（来源事件上保留的已派出任务）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

容量和调度规则为可运行的简化模型，不接入闸机、视频分析、室内定位、消防联动或真实应急指挥系统。
