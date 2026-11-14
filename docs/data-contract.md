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
