# Market环境1 Harbor与Forgejo配置

Harbor使用官方Helm Chart 1.19.4（Harbor 2.15.4），运行在192.168.10.10的`market-registry`命名空间。访问地址为`https://192.168.10.10:30443`。`market`项目为私有项目，配额8 GiB。PVC使用现有`local-path`存储，部署采用单节点开发配置，初始关闭Trivy以适配共享节点资源。

## Forgejo仓库配置

仓库`hxyl/OperationPlatform`当前已启用Actions。以下三项由仓库管理员操作。可使用本仓库的设置页面，无需编辑Forgejo服务端`app.ini`。

1. 在`Settings → Actions → Variables`设置`HARBOR_REGISTRY=192.168.10.10:30443`、`HARBOR_PROJECT=market`。仓库地址填写主机和端口，不带`https://`或项目路径。
2. 在`Settings → Actions → Secrets`设置`HARBOR_USERNAME`和`HARBOR_PASSWORD`，分别取环境1服务器`/root/market/ops/harbor-env1/forgejo-publish.json`里的`name`和`secret`。该机器人账号只有Market项目的推送和拉取权限，有效期180天。
3. 在`Settings → Actions → Runners`创建仓库级Runner，建议命名`market-env1-builder`，保留页面生成的UUID和Token。登录192.168.10.10执行下方脚本，按提示录入。执行完成后在Runners页面核对Online状态和`market-build`标签。

```bash
bash /root/market/ops/ci-env1/configure_market_forgejo_runner.sh /root/market/ops/ci-env1
```

普通仓库变量由工作流`${{ vars.X }}`引用，机器人账号及密钥由`${{ secrets.X }}`引用。Runner Token以Kubernetes Secret保存。证书和账号材料均无需录入代码。用于申请复制制品的只读机器人凭据单独保存在`/root/market/ops/harbor-env1/replication-read.json`，环境2联调时使用。

## 工作流与首轮验证

工作流为`.forgejo/workflows/market-images.yml`。受保护`main`中影响镜像的文件变更会触发构建，Actions页面也允许从`main`手动触发。每次构建为6个Market镜像使用同一`YYYYMMDDHHMMSS-GitSha`标签，输出镜像摘要。环境2的OCI发布包与Flux由后续环境2联调接入。

Runner采用官方镜像，任务的`host`执行范围是专用Runner容器，使用同Pod的非root BuildKit。只有经审核的仓库维护者应使用该构建能力。Runner无宿主机容器运行时Socket和集群凭据，BuildKit只监听Pod本地端口。

先完成仓库配置，再合并包含工作流的分支`codex/harbor-forgejo-env1-20261118`至`main`。首轮联调依次检查Runner在线、仓库构建任务成功、Harbor可见同标签的6个镜像、输出摘要和Git提交一致。Runner在线和真实构建任务需要上述仓库管理员配置完成后验证。

当前验证结果为Harbor七项组件健康检查通过，私有项目的写入账号推送测试镜像、只读账号拉取成功。Kubernetes中的非root BuildKit已实际执行Dockerfile的RUN步骤并推送测试镜像。原Helm安装在首次拉取镜像和任务服务就绪时超时，待依赖就绪后已完成恢复，当前Helm状态为`deployed`。Forgejo现有账号访问Runner、变量和Secrets管理接口均为403，自动构建仍需仓库管理员完成配置。

## Harbor证书与运维

Harbor使用自建CA，公开CA证书位于`/root/market/ops/harbor-env1/ca.crt`，CA私钥和管理员密码文件应保存在服务器的私有目录。外部客户端和后续环境2 Harbor应导入该CA。管理页面账号为`admin`，初始化随机密码在`/root/market/ops/harbor-env1/admin-password`。

Harbor状态可从目标节点执行以下命令检查：

```bash
helm status harbor -n market-registry
kubectl get pods,pvc -n market-registry
curl --fail --cacert /root/market/ops/harbor-env1/ca.crt \
  https://192.168.10.10:30443/api/v2.0/health
```
