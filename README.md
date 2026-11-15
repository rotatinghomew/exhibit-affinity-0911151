# 跨馆展品目录、展示机会和个性化候选推荐

exhibit-affinity 是 Python 业务分析应用，提供原始资料核对、全站概览、个性化候选推荐、按时间回看的策略比较和实际选品发布。摘要只说明观测记录本身；个性化结论严格区分已被展示验证的信号与没有曝光机会、离线未验证的冷启动内容。

## 目录和边界

`exhibit_affinity/data.py` 读取资料并冻结公开日期可见的版本。`product.py` 实现全站概览，`recommend.py` 实现个性化候选（`personalized-candidates-v1`），`backtest.py` 实现按时间滚动回看的策略比较（`temporal-backtest-v1`），`selection.py` 把推荐接到实际选品名单并处理授权收回（`personalized-selection-v1`），`workflow.py` 串联资料读取与成果发布，`service.py` 提供本地 HTTP 资料和消费接口，`store.py` 保存不可变成果和当前消费约束，`client.py` 是调用适配层。服务接口见 `docs/protocol.md`，具体资料含义见 `docs/data-contract.md`。

## 推荐与回看口径

- 个性化信号只来自真实展示机会之后的编辑操作（open/save）；未被展示的作品没有反馈、不当负样本，只能走基于目录属性的 `catalog-only` 冷启动并标记 `offline_validated=false`。新编辑退回全站先验加内容相似，结果确定且可解释。
- 离线评估不使用随机切分：按 slate 发生时间滚动切分，训练资料公开日期严格早于测试集合；每个评估单元的候选只包含该真实展示集合内当时资格有效的作品，标签是之后真实发生的操作。
- 成套作品（family）通过整组留出折保证不被拆到训练和验证两边；没有任何曝光机会的 family 在 `unexposed_segment` 中显式标为未验证，离线收益不得解释为已验证。
- propensity 不是随机试验概率，只作为带截断的 IPS 诊断量；策略取舍以未加权排序指标（precision/recall/NDCG/MRR）对全站热门与随机基线的逐折比较为准。
- 授权服务可在取单后收回资格：最终 `items` 服从提交时最新 policy，被收回作品从名单移除但原始推荐依据保留在 `withdrawn_by_policy`；代次冲突按最新 policy 重试，服务端禁用校验是最后防线。

## 环境和依赖

兼容 Python 3.11.x，具体范围见 `pyproject.toml`；`.python-version` 固定受支持的次版本，不限定补丁版本。独立 Docker 镜像使用固定摘要的 Python 3.11.14，生产验证容器使用 Python 3.11.2，两者采用相同的完整锁定依赖。`requirements.lock` 与 `requirements.txt` 固定数值计算和模型分析的完整依赖版本。应用运行、测试及资料均在本地，不请求外部 API；构建时预装依赖，正式运行不安装或下载资料。

```bash
docker build -t exhibit-affinity:local .
docker run --rm --network none exhibit-affinity:local python tools/demo.py
docker run --rm --network none exhibit-affinity:local python -m unittest discover -s tests -v
```

已有 Python 环境可以先执行 `python -m pip install -r requirements.lock`，然后执行以下检查与产品演示：

```bash
python -m compileall -q exhibit_affinity tests tools
python -m unittest discover -s tests -v
python -m build --wheel --no-isolation
python tools/demo.py
```

演示会启动真正的本地 HTTP 服务，读取业务资料，依次发布全站概览、时序回看证据和个性化选品成果并读回，结束时自动退出。每次使用临时数据库，不覆盖工作目录。

## 交互服务

```bash
python -m exhibit_affinity.service --port 8765
```

默认服务地址为 `127.0.0.1:8765`，本地成果保存在 `local-state.sqlite3`。启动后资料只读；本地消费约束可通过管理接口变更，不会修改已发布成果。中断运行可正常退出。方法版本各自独立标明：`observed-overview-v1`（全站概览）、`personalized-candidates-v1`（个性化候选）、`temporal-backtest-v1`（时序回看证据）、`personalized-selection-v1`（实际选品）。语义变化必须使用新方法名，不能复用旧方法名掩盖。
