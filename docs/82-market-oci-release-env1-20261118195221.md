# Market 环境1构建与 OCI 发布包

## 范围

环境1独立 BuildKit 构建已合并到 Forgejo `main` 的固定 Git 提交。验证制品存放在 `192.168.10.10:30443/market-ci-verify` 私有项目，使用项目级机器人账号。Forgejo 作业领取与环境2部署仍属于后续联调。

## 本次验证结果

- Git 提交 `1ec0ef738fd76e631e29ceb7d992360aa2eefc7b`，发布版本 `20261118193421-1ec0ef7`。六个实际构建镜像的摘要见同版 `release.json`。
- OCI 包 `oci://192.168.10.10:30443/market-ci-verify/market-release@sha256:018de9529fc26e14155aa4c075bf63712605de0aae5704c861106c0634f1047c`，标签与镜像标签一致。已按摘要重新拉取并逐文件比对。
- 通过七项定向测试，验证元数据缺失、混合版本、清单篡改、摘要绑定、镜像缺失、摘要不符，以及重复、冲突和乱序通知。
- 使用实际 Harbor 测试，六个标签及摘要引用全部可读。测试中的缺失标签全部阻断，故意提供的摘要不一致也被拦截。重复检查得到 `duplicate`，仍只记录一个已验证版本。
- 使用 `kubectl kustomize` 渲染五个阶段共18个资源，映射到模拟目标仓库后镜像摘要保持一致。该检查验证清单渲染，未将清单应用到集群。
- 首次构建遇到 Python 公共仓库下载缓慢，API、网关和两个模拟服务在隔离构建副本中改用阿里云 PyPI 镜像，应用源码及声明的依赖版本来自上述提交。API 两次解析的38个包版本一致。构建日志和依赖源变更记录保存在环境1的 `/root/market/ops/ci-env1/first-release-1ec0ef7`。
- 临时 BuildKit Pod 已清理。验证项目配额4 GiB，保留本次版本用于后续同步演练。

## Forgejo 自动发布

`main` 的 `Publish Market images and OCI release` 工作流先推送六个镜像，再使用同一次构建的 BuildKit 元数据和 Git 提交中的 Kubernetes 清单生成发布包。作业使用已有 Harbor 地址、项目和机器人凭据，将制品推送到 `market/market-release:<版本>`。工作流按制品摘要重新拉取并逐文件核对，成功日志包含 `OCI release payload re-pulled and verified`。

打包工具在同一个 BuildKit Pod 中按 `k8s/ci/Dockerfile.release` 运行。仓库 CA 和机器人凭据由 BuildKit secret 挂载。K8S2 可先配置镜像和发布包的 Harbor 同步。Flux 部署前，使用目标 Harbor 的只读凭据确认发布包引用的镜像标签和摘要均已同步。

## 形成发布包

六个镜像的标签沿用 `YYYYMMDDHHMMSS-GitSha`。构建脚本设置 `MARKET_IMAGE_METADATA_DIR` 时，在所有镜像推送成功后导出六份 BuildKit 元数据。`market_release_bundle.py` 从指定提交读取已纳入发布范围的 Kubernetes 清单，将镜像转换成 Harbor 摘要引用。

```bash
python3 scripts/market_release_bundle.py \
  --repo . --commit "$commit" --version "$tag" \
  --registry 192.168.10.10:30443 --project market-ci-verify \
  --metadata "$metadata" --output "$bundle"
```

`release.json` 包含提交、版本、六个镜像摘要、清单校验值、阶段依赖及环境需预置的资源。应用清单分为 API、Worker、Web/网关、业务 CronJob、可选模拟服务。现有数据库、Redis、MinIO、APISIX、PVC、Service、环境配置及 Secret 由目标环境提供。包内的 Secret 项为资源引用。备份和停启等有状态操作沿用运维流程。

在能访问验证 Harbor 的主机上使用 Flux CLI 2.10.0，以及 `PyYAML` 和现有 Harbor CA。用验证项目的机器人凭据文件执行发布脚本。

```bash
MARKET_FLUX=/root/market/ops/ci-env1/flux-2.10.0 \
  bash scripts/publish_market_release_bundle.sh "$bundle" \
  192.168.10.10:30443 market-ci-verify "$publish_robot_json" "$ca_crt"
```

发布脚本先验证仓库中全部镜像的版本标签及摘要，再通过 `flux push artifact` 发布 OCI 制品。上传后用制品摘要重新拉取，逐文件核对内容。案例中的 Harbor 地址应随部署环境配置。

## 目标端检查

`verify_market_release.py` 可用目标 Harbor 只读凭据独立执行。它校验包内清单与镜像摘要的对应关系，访问目标仓库验证每个标签和摘要引用。`--state` 用于记录该目标当前已验证的版本，识别重复、乱序和同版本不同制品。该记录只表示验证通过，后续部署控制器需要单独维护部署结果。

```bash
python3 scripts/verify_market_release.py --bundle "$pulled_bundle" \
  --registry "$target_registry" --project "$target_project" \
  --auth-file "$readonly_robot_json" --ca "$ca_crt" --state "$verification_state"
```

接入环境2时，在 Flux `Kustomization` 的镜像映射中将包内源仓库名映射到目标 Harbor 仓库名，保留镜像摘要。使用阶段依赖前，应将本次版本、API 实际就绪与目标镜像已全部到齐设为更新条件。Webhooks 触发检查的部署控制层与真实跨 Harbor 传输均待环境2联调。
