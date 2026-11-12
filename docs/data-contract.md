# 分层汇总资料

tree是公开的有根树，每个叶节点深度相同，depth从根0开始。epsilon_by_depth与树深度对应，分数字符串表示精确预算，不使用二进制浮点累加判断超支。每人的贡献选择按(event_day,event_id)升序取第一条，不按类别或事件结果决定排序。叶类即使无人出现仍存在于公开tree。

privacy_scope覆盖固定合成人群；dataset_version只标资料版本，不重置scope_budget。release_id是scope内幂等业务身份。演示模式与正式发布模式属于不同输出类别，demo_seed仅在明确的demonstration材料中使用。账本的输出回执不含原始记录。

当前domain核对树和叶引用，给出原始资料人数及事件数；这些原始摘要供本地维护，不能当作加噪发布结果。

加噪发布由 `research_data.publish` 编排：`tree.py` 解析公开树与公开遍历顺序，`selection.py` 完成每人一条事件的贡献收缩，`mechanism.py` 按层独立加 Laplace 噪声（演示种子仅在 `mode=demonstration` 且带 `demo_seed` 时允许；`mode=publication` 拒绝任何种子并改用 `secrets` CSPRNG），`consistency.py` 做带权非负最小二乘一致化，`ledger.py` 按 scope 记精确分数预算。对外回执只含公开树、加噪/一致化浮点计数、每层 epsilon、尺度与方差、目标与残差、预算快照和 receipt_hash；subject_id、真实计数与种子一律不得出现。回执机制与账本一致性的数学论证见 [privacy-proof.md](privacy-proof.md)。
