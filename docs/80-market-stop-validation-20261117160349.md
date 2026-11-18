# Market停用与脚本验证记录

## 实际结果

- 环境192.168.10.10，仅处理market命名空间。
- 21个Deployment已缩容至0；24个原运行Pod停止；18个原临时Pod清理。
- 10个CronJob全部暂停，避免定时重建Pod。
- 核验market没有任何Pod，7个PVC的名称和UID与停用前完全一致，Namespace、Service及业务数据未删除。
- Metrics API未安装，本轮不虚报实际CPU/内存使用量；以Pod全部退出、工作负载副本为0确认资源不再被Market容器运行占用。

## 数据与计费保护

停用前先关闭业务入口及计算Worker，通过维护API提交inspect和prepare任务。确认来源已封存且校验结果safe_sealed_resume_candidate后才停止API、Redis等核心组件，PostgreSQL最后停止。

主机状态及原始资源快照：

```text
/root/market/backups/market-stop-20261117155544-e41edf/state.json
```

主机归档database-before.dump已完整解析验证且SHA256与状态记录一致，不只是检查文件非空或目录列表；未向业务数据库执行恢复。维护任务自身的备份/审计还保存在维护PVC和PostgreSQL。未建立新计费分代或把未知用量按零处理。

最初以标准输入执行pg_restore --list时发生流传输超时，停止流程按保护规则保留核心存储运行。修正为把归档复制到PostgreSQL容器的唯一临时文件，再执行pg_restore --file=/dev/null完整解析、不执行SQL；验证后删除容器临时归档，继续完成安全封存和停用。原始恢复副本数快照未被中途的0副本覆盖。

## 脚本与验证

三个配套文件已安装到/root/market/ops/project-control，权限仅root：

- stop-market.sh：停止项目。
- start-market.sh：按原副本数和依赖顺序恢复，校验计费封存后再开放业务入口。
- market_project_control.py：共享控制器，不能与两个Shell入口分开放置。

7项离线停止/启动流程测试通过；实际stop脚本执行完成。重复stop验证为无副作用的已停止状态；start --dry-run完成UID及恢复信息核对，未执行真实启动，因此目前仍保持0 Pod，不重新占用资源。

真实启动、跨日停机恢复、外部主机故障和异常缺口接受流程未在本轮实际执行。未来校验失败时，脚本默认保留业务入口关闭，不应盲目使用--accept-gap。显式接受缺口必须人工核查并填写理由。

使用方法见79号说明文档。最终发布包为market-project-control-20261117160349.tar，旧的初版脚本包已撤下，避免误用未经修正的备份校验方式。
