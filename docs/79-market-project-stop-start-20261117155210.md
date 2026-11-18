# Market项目停止与启动脚本

## 使用位置

192.168.10.10上以root执行，三个配套文件固定安装在/root/market/ops/project-control：stop-market.sh、start-market.sh、market_project_control.py。Shell入口与Python控制器必须放在同一目录。

```bash
# 仅显示停止计划，不执行修改
/root/market/ops/project-control/stop-market.sh --dry-run

# 停止Market，保留数据和恢复信息
/root/market/ops/project-control/stop-market.sh --reason '暂时停用Market，释放CPU和内存'

# 后续恢复原副本数并重新开放服务
/root/market/ops/project-control/start-market.sh --reason '恢复Market项目运行'
```

## 停止动作

1. 只处理market命名空间，校验目标集群包含192.168.10.10节点；保存Namespace UID、各Deployment/StatefulSet的原始副本数、CronJob原暂停状态及资源清单。
2. 暂停CronJob，暂停仍活跃的Job，关闭Web/网关与非核心计算服务；等待交付/升降级/退款Worker正常退出。
3. 通过API容器内部提交只读来源检查；来源正常时生成主机侧PostgreSQL自定义归档并验证，再通过既有维护任务执行备份、排空和封存。已经存在有效封存证据时保留该证据并备份，不重复换代。
4. 封存校验通过后停止维护执行器、计费采集、API，再停止Redis/MinIO/etcd等依赖，最后停止PostgreSQL。
5. 删除market剩余临时Pod，复查没有Pod被重建。所有业务数据PVC、Service、Namespace和控制器定义均保留。

不是简单delete运行中的Pod，避免Deployment自动补回。不会处理其他项目、修改kube-system控制面etcd，也不使用强制删除或删除PVC。

## 启动动作

1. 从主机持久状态文件恢复准确副本数，不统一猜测为1；校验Namespace和控制器UID，拒绝把旧快照套用到被重建的资源。
2. 先启动PostgreSQL、项目etcd、MinIO和Redis，再启动API及维护执行器。
3. 启动计费采集Worker但先不要求其就绪，通过维护任务检查封存证据并resume原分代，随后确认采集恢复就绪。
4. 校验正常后恢复其他服务，最后开放Web与网关，再恢复CronJob原暂停状态。

恢复校验不通过时，默认不打开业务入口，也不自动建立计费新分代。跨日停机可能因带有效期的证据变化而无法证明连续性；人工检查并接受未知缺口后，才可执行：

```bash
/root/market/ops/project-control/start-market.sh \
  --accept-gap --reason '已审查停机期间证据，确认保留未知计费缺口并恢复新分代'
```

本参数只在实际需要缺口时执行recover，不覆盖历史游标或清零配额。

## 定时任务与失败处理

启动可暂不恢复定时任务，便于先人工核查：

```bash
/root/market/ops/project-control/start-market.sh --keep-cronjobs-suspended
```

确认后再运行正常start命令恢复原CronJob状态。解除暂停可能触发K8S补调度，需注意历史任务影响。停止时已活跃而被暂停的旧Job不会自动重放；需要时按Job清单单独审核恢复，避免不明副作用。临时/独立调试Pod不会自动重建。

- 所有恢复信息保存在/root/market/backups/market-stop-时间-随机后缀/state.json，最新状态指针位于/root/market/ops/project-state/latest.json，均在命名空间之外。
- 需要指定某次状态时，使用--state /root/market/backups/market-stop-xxx/state.json，不要删除或改写原始副本数。
- 维护任务、数据库归档和来源报告按root权限保存，脚本不保存或打印登录Token/SSH密码。
- 若封存/备份失败，停止流程中止，核心存储保持运行供核查；若启动校验失败，可能已启动基础服务，但业务入口仍保持关闭。
- 使用主机排他锁，禁止同时运行两个项目控制脚本；也不要与网页维护或其他直接CLI维护并行执行。
- 当前部署使用Deployment；如果将来增加DaemonSet或HPA，脚本会在修改前拒绝，需要先补充对应停用策略。
- 7项离线流程测试验证了封存前不关闭Redis、数据库最后关闭、启动时入口最后开放、无证据不启动业务及显式缺口恢复等保护。真实启动不应在本次资源释放后立即执行，否则会重新占用资源。
