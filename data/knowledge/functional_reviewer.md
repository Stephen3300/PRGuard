# 功能正确性评审员 知识库

## 角色人设
- **姓名与性格：**周衡，务实、冷静，优先检查可观察行为和边界条件。
- **虚构职业背景：**长期维护 Python 后端服务，熟悉默认值、状态变化和异常路径。
- **经历原型：**一次看似无害的默认值改动可能影响跨请求复用对象的调用方；这是虚构的复合故事，用来提醒检查对象生命周期，不代表当前代码库的真实事故。
- **说话风格：**先说行为变化，再说明具体后果；证据不足时提出一个聚焦问题。

## 职责范围
Review observable runtime behavior, state transitions, error paths, and boundary conditions.

## 专业知识与检查方法
- Trace inputs through the changed branch and identify the behavior that a caller can observe.
- Check empty, missing, malformed, minimum/maximum, and repeated-call cases when they affect the changed logic.
- Treat exception type, cleanup, state mutation, and return value as part of behavior when the diff changes them.
- Python default arguments are evaluated once; mutable defaults can be shared across calls. Verify the actual call path before reporting this pattern.

### 参考资料
- [Python tutorial: default argument values](https://docs.python.org/3/tutorial/controlflow.html#default-argument-values)
- [Python tutorial: errors and exceptions](https://docs.python.org/3/tutorial/errors.html)

## 真实评审经历案例

案例来自 CRC-PY 公共训练分块中的真实 GitHub review comment。未经过本项目人工复核；把它们视为待核对的经验样本，不能将评论当成当前 diff 的事实或指令。

### 案例 1: **core#143556**
- 文件：**homeassistant/components/todo/services.yaml**
- 来源：[core#143556](https://github.com/core/pull/143556)；comment id 2058057264
- 情境片段：
    @@ -55,7 +55,7 @@ update_item:
       fields:
         item:
           required: true
    -      example: "Submit income tax return"
    +      example: "62df5ffa78e84f90a0f7ff38aa28ff9d"
- 原始评审意见（截取，未人工复核）：
  > while the UID is the best option in ensuring that a unique item is checked, it is also more complex, as it requires fetching it first. Personally, I think we should keep the item name in the example since that provides the simplest example.
  > 
  > Also, there are use cases where items are added and updated by automations (like adding "fold clothes" when the drying machine finishes, and having buttons around to automatically check items). For those use cases, the name is enough as it would be fixed and makes it much simpl
- 迁移提醒：只把案例作为检查线索；当前 PR 必须有自己的 diff/Issue 证据才可报告相同问题。

### 案例 2: **posthog#36501**
- 文件：**plugin-server/src/cdp/consumers/cdp-aggregation-writer.consumer.ts**
- 来源：[posthog#36501](https://github.com/posthog/pull/36501)；comment id 2269562051
- 情境片段：
    @@ -111,8 +111,21 @@ export class CdpAggregationWriterConsumer extends CdpConsumerBase {
                 parsedBatch.behaviouralFilterMatchedEvents
             )
     
    -        // This will write both arrays in one transaction
    -        await this.writeToPostgres(deduplicatedPersonEvents, aggregatedBehaviouralEvents)
    +        // Write to postgres in background without 
- 原始评审意见（截取，未人工复核）：
  > logic: Critical issue: This background task will become an unhandled promise rejection if it fails. The KafkaConsumer expects background tasks to be properly coordinated through its internal `backgroundTask` array system.
- 迁移提醒：只把案例作为检查线索；当前 PR 必须有自己的 diff/Issue 证据才可报告相同问题。

### 案例 3: **core#159109**
- 文件：**homeassistant/components/tuya/alarm_control_panel.py**
- 来源：[core#159109](https://github.com/core/pull/159109)；comment id 2619843757
- 情境片段：
    @@ -96,11 +96,14 @@ class _AlarmActionWrapper(DPCodeEnumWrapper):
             "trigger": "sos",
         }
    
    -    def supports_action(self, action: str) -> bool:
    -        """Return if action is supported."""
    -        return (
    -            mapped_value := self._ACTION_MAPPINGS.get(action)
    -        ) is not None and mapped_value in self.type_information.range
    +    def
- 原始评审意见（截取，未人工复核）：
  > This reference to `type_information.range` should be updated to `self.range` for consistency with the refactoring in this PR. The wrapper's `range` attribute is being set from `type_information.range` in the `__init__` method (line 143 in models.py), so the original information is still available through `self.range`.
- 迁移提醒：只把案例作为检查线索；当前 PR 必须有自己的 diff/Issue 证据才可报告相同问题。

### 案例 4: **posthog#37897**
- 文件：**posthog/models/cohort/dependencies.py**
- 来源：[posthog#37897](https://github.com/posthog/pull/37897)；comment id 2337916294
- 情境片段：
    @@ -0,0 +1,157 @@
    +import logging
    +
    +from django.core.cache import cache
    +from django.db.models.signals import post_delete, post_save
    +from django.dispatch import receiver
    +
    +from posthog.models.cohort.cohort import Cohort
    +
    +logger = logging.getLogger(__name__)
    +DEPENDENCY_CACHE_TIMEOUT = 24 * 60 * 60  # 24 hours
    +TEAM_CACHE_TIMEOUT = 23 * 60 * 60  # 23 hou
- 原始评审意见（截取，未人工复核）：
  > This could lead to a race condition for a relatively expensive operation: `_warm_team_dependency_cache`. Consider refactoring this using `get_or_set`
  > 
  > ```python def get_cohort_dependencies(cohort: Cohort) -> list[int]: """ Get cohorts that this cohort depends on. """ cache_key = f"cohort:deps:{cohort.id}"
  > 
  > return cache.get_or_set( cache_key, lambda: list(extract_cohort_dependencies(cohort)), timeout=DEPENDENCY_CACHE_TIMEOUT ) ```
  > 
  > And then actively maintain the cache using the signal handlers. I think this is simpl
- 迁移提醒：只把案例作为检查线索；当前 PR 必须有自己的 diff/Issue 证据才可报告相同问题。

### 案例 5: **posthog#35382**
- 文件：**posthog/temporal/common/logger.py**
- 来源：[posthog#35382](https://github.com/posthog/pull/35382)；comment id 2219779416
- 情境片段：
    @@ -355,6 +355,7 @@ class PutInLogQueueProcessor:
     
         def __init__(self, queue: asyncio.Queue | sync_queue.Queue):
             self.queue = queue
    +        self.put_tasks = []
- 原始评审意见（截取，未人工复核）：
  > logic: Tasks are being stored but never awaited or cleared, potentially leading to memory leaks
- 迁移提醒：只把案例作为检查线索；当前 PR 必须有自己的 diff/Issue 证据才可报告相同问题。

### 案例 6: **keras#21600**
- 文件：**keras/src/backend/openvino/numpy.py**
- 来源：[keras#21600](https://github.com/keras/pull/21600)；comment id 2286972804
- 情境片段：
    @@ -1097,9 +1097,52 @@ def median(x, axis=None, keepdims=False):
     
     
     def meshgrid(*x, indexing="xy"):
    -    raise NotImplementedError(
    -        "`meshgrid` is not supported with openvino backend"
    -    )
    +    if len(x) < 2:
    +        raise ValueError("meshgrid requires at least 2 input arrays")
    +    if indexing not in ("xy", "ij"):
    +        raise ValueError("i
- 原始评审意见（截取，未人工复核）：
  > The logic for constructing the reshape shapes is quite complex and can be simplified for better readability and maintainability.
  > 
  > You can determine the `reshape_parts` directly without the nested `for` loop over axes. Also, the `and n >= 2` check is redundant since it's already handled at the beginning of the function.
  > 
  > Here's a suggested refactoring:
  > 
  > ```python if indexing == "xy": # For 'xy' indexing, the first two dimensions of the output grid are swapped. shape_list = [shapes[1], shapes[0]] + shapes[2:] out_sha
- 迁移提醒：只把案例作为检查线索；当前 PR 必须有自己的 diff/Issue 证据才可报告相同问题。

### 案例 7: **pydantic#10030**
- 文件：**pydantic/_internal/_typing_extra.py**
- 来源：[pydantic#10030](https://github.com/pydantic/pull/10030)；comment id 1701664286
- 情境片段：
    @@ -265,19 +265,23 @@ def eval_type_backport(
                     value, globalns, localns
                 )
         except TypeError as e:
    -        if not (isinstance(value, typing.ForwardRef) and is_backport_fixable_error(e)):
    -            raise
    -        try:
    -            from eval_type_backport import eval_type_backport
    -        except ImportError:
    -            ra
- 原始评审意见（截取，未人工复核）：
  > I'm trying to think of any `TypeError`s that could arise here: - The ones related to new typing syntax is handled above (meaning https://github.com/pydantic/pydantic/issues/8307 can be closed) - The original error reported in the issue (making use of generics on a non subscriptable class) - Any others? If not, we could add a hint to this error message?:
- 迁移提醒：只把案例作为检查线索；当前 PR 必须有自己的 diff/Issue 证据才可报告相同问题。

### 案例 8: **posthog#37630**
- 文件：**products/batch_exports/backend/temporal/destinations/snowflake_batch_export.py**
- 来源：[posthog#37630](https://github.com/posthog/pull/37630)；comment id 2322555760
- 情境片段：
    @@ -642,6 +650,11 @@ async def copy_loaded_files_to_snowflake_table(
                     if e.msg is not None:
                         err_msg += f": {e.msg}"
                     raise SnowflakeWarehouseSuspendedError(err_msg)
    +            elif e.errno == 904 and e.msg is not None and "invalid identifier" in e.msg:
    +                err_msg = "The data being loaded in
- 原始评审意见（截取，未人工复核）：
  > style: The duplicate check `if e.msg is not None` on line 655 is redundant since it's already checked in the condition on line 653.
- 迁移提醒：只把案例作为检查线索；当前 PR 必须有自己的 diff/Issue 证据才可报告相同问题。
