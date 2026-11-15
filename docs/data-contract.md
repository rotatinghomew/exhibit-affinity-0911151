# 业务资料契约

impressions 是确实发生的展示机会，slate 标识一次候选集合，propensity 是日志记录的机会概率，不代表完整随机试验。actions 引用 impression，open 与 save 是不同信号。items.family 将同系列作品关联；属性向量是目录描述，不是个性化模型结果。eligibility 表是历史资格，当前提交资格来自消费端 policy。

## 资料表

| 表名 | 原始记录数 | 字段 |
| --- | ---: | --- |
| items | 72 | attributes, family, id, language, museum, released_at, revision |
| impressions | 480 | id, item, propensity, released_at, revision, slate, user |
| actions | 99 | action, id, impression, item, released_at, revision, user |
| eligibility | 73 | allowed, id, item, released_at, revision |

记录均为本地合成资料，只表达业务结构，不是公开报道所述人群或机构的实测资料。日期是本地资料日历；所有版本以资料服务的 as_of 为准。字段值和自然缺失保留原义。金额、评分、频率和权重不可跨表不加判断地直接合计。

## 建模口径

- actions 只在引用的 impression 存在且 user/item 一致时有效；open 与 save 是不同强度的正向信号（save 权重高于 open）。没有对应 action 的 impression 是未反馈的展示，不构造成负样本。
- 一个作品若从未出现在 impressions 中，则没有任何反馈可学：不进入用户偏好统计、不获得全局先验，只能依据 items.attributes 的内容相似与 museum/language 目录字段走冷启动，结果标记 `catalog-only` 且 `offline_validated=false`。
- items.family 是成套作品分组。离线评估的整组留出以 family 为单位：被留出 family 的全部 impressions 与 actions 同时退出训练，避免同系列另一半作品泄漏到验证侧。
- impressions.propensity 是日志记录的机会概率，不代表完整随机试验；只允许作为带截断的 IPS 诊断量，策略取舍以未加权的排序指标为准。
- eligibility 是历史资格视图，会随修订代次变化（如 item-004 从 allowed 修订为撤回）。当前提交资格以消费端 policy 的最新代次为准，历史回看按各日期快照重建当时资格。
