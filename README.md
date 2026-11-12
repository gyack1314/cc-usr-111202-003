# 合作研究计数发布的分层隐私预算

这是研究资料维护用的独立 Python 项目。`data/` 保存合成记录，`research_data.io`
负责严格 JSON 读取与 SHA-256 来源摘要，`research_data.domain` 提供当前原始资料
统计，`research_data.cli` 输出摘要或生成一次性分层加噪发布回执。字段、时间、
计数和量纲见[资料约定](docs/data-contract.md)，隐私机制与账本一致性论证见
[隐私论证](docs/privacy-proof.md)。所有研究、中心及受试者均为虚构。

Python 3.11 及以上版本可直接使用，当前没有第三方依赖。在项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q research_data tests
python -m research_data.cli inspect data/normal.json
```

## 分层加噪发布

每人按公开规则 `(event_day, event_id)` 升序只保留一条入选事件；相邻数据集定义
为加入或删除一个人的全部记录，因此每个树层向量的 L1 敏感度为 1。每层按事先
登记的正 `epsilon_level` 独立加入尺度 `1/epsilon_level` 的 Laplace 噪声
（真计数为零的叶也加噪），总 epsilon 为各层之和；随后在全部节点上做带权非负
最小二乘一致化（Lawson–Hanson 主动集法），输出连续实数计数及可重算目标与
最优性残差。一致化只使用已加噪值，是差分隐私后处理，不强制根等于真实总数。

```sh
# 演示资料（固定种子，回执标记 publishable=false，禁止对外发布）
python -m research_data.cli release data/normal.json \
  --ledger /tmp/research/ledger.sqlite --receipt-out /tmp/research/r1.json --recompute

# 正式发布：mode=publication，使用操作系统 CSPRNG（secrets），无种子、不可复现
python -m research_data.cli release /path/to/publication-request.json \
  --ledger /tmp/research/ledger.sqlite

# 查看 scope 累计预算与发布清单
python -m research_data.cli ledger-status --ledger /tmp/research/ledger.sqlite \
  --privacy-scope synthetic-cohort
```

预算按固定人群的 `privacy_scope` 累计，换数据版本或文件名不重置额度；
`release_id` 是幂等键：同 ID 同请求重取同一份加噪结果（不重新抽样、不重复
扣款），内容或机制参数冲突则拒绝。预留与建单在单个 `BEGIN IMMEDIATE` 事务内
完成，并发请求在写锁上串行校验余额，不会超支；事务提交后的投递失败不回滚
预算，用同一 ID 重试即可取回完全相同的回执。回执把可见数字、噪声参数与预算
快照绑定在同一文档并附 `receipt_hash`，不含 subject_id、真实计数或随机种子。

`inspect` 只显示已有本地摘要，不修改来源。文件名以 `invalid-` 起始的资料包含
故意不合法的输入，其他文件是同结构的业务边界。数据摘要与来源指纹应同时保留，
原始 JSON 不覆盖。

可使用固定镜像容器：`docker build -t research-material .`，随后
`docker run --rm research-material` 查看资料；
`docker run --rm research-material python -m unittest discover -s tests -v`
运行全部检查。全部资料在项目内，运行不需要外部账号或服务。
