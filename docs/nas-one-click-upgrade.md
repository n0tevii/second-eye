# 群晖手动一键升级（准备阶段）

目标是先在 GitHub 构建并测试 Linux amd64 镜像，NAS 只下载镜像。
首次配置完成后，在 DSM 任务计划程序手动运行一次升级任务。
固定版本和 sha256 digest 由每次已批准的发布指定，不追踪 latest，不定时自动升级。

当前状态：脚本通过本地模拟 Docker 的检查；未在 NAS 执行，Linux 镜像构建及
实际升级/回滚仍为 NOT_VALIDATED。GitHub 连接缺少 workflow scope，工作流尚未上传。

## 发布流程

`.github/workflows/publish-image.yml` 接收已存在的正式 release tag；读取该 tag 的源码，
记录源码提交及基础镜像 digest，构建 amd64 镜像。回归测试、鉴权/跨站保护、
关闭 noVNC 和 Chromium 启动检查通过后，才推送 GHCR。
不覆盖已有版本 tag。release 附件记录最终 image digest 和构建清单。
应用镜像没有测试依赖、运行数据库、Cookie 或 .env。

上传工作流需要当前 GitHub 连接获准增加 workflow scope。首次 GHCR 包还需要核实
可见性及 NAS 拉取权限；不能以 Actions 成功代替匿名拉取验证。

## 首次安装前必须核对

只适用于 `/volume1/docker/second-eye` 单服务项目，现有容器 `second-eye` 必须运行。
核实当前实际 Compose 文件名（compose.yml 或 docker-compose.yml）、镜像 ID、
数据挂载和数据库路径。逐项比较 `deploy/compose.image.yml` 与生产配置；有自定义
网络、挂载或服务时不能直接套用模板。

审批时列明：目标版本和 digest；项目/tools/backups 目录；仅本机监听
18000、16080（noVNC 关闭）；内存 3 GB、共享内存 512 MB；不设 CPU 硬限额；
镜像和备份增加的磁盘空间；GitHub Actions/存储是否涉及账户费用。
没有核实用量和账户额度前不承诺免费或确定升级耗时。

生产批准后，才安装以下文件到项目的 tools 目录：

- `scripts/nas_upgrade.sh`
- `scripts/upgrade_db.py`
- `deploy/compose.image.yml`

脚本和模板由管理员控制，运行应用不能修改升级脚本。DSM 手动任务需要 Docker
管理权限，这属于首次新增权限，需用户明确批准。不会挂载 Docker socket 给应用，
不会调整其他项目、反向代理、路由器、防火墙或公网映射。

管理员核实 Docker CLI/Compose 可用后，任务的形式如下（占位 digest 不可直接运行）：

```sh
/bin/sh /volume1/docker/second-eye/tools/nas_upgrade.sh upgrade /volume1/docker/second-eye compose.yml v0.2.4 ghcr.io/n0tevii/second-eye@sha256:REPLACE_WITH_VERIFIED_DIGEST
```

将 upgrade 换成 check 可做配置预检；这会由 Compose 读取 .env，但不输出凭据，
不下载、不停容器、不更改数据。预检不能证明目标镜像可用。

## 升级和失败处理

脚本先下载并校验镜像平台、版本，再停止本项目容器。保存 Compose、.env、
旧镜像 ID 和停止状态下整个 data 的 tar；备份目录仅管理员可访问。
随后暂停数据库中已启用的任务，启动新镜像完成迁移，检查健康接口、版本及
实际镜像 ID，最后恢复原先启用的任务。禁用任务保持禁用。

下载失败不停止旧容器；备份失败尝试启动未变更的旧容器。
迁移或启动检查失败时停止本项目，保留失败数据到备份目录的 failed-data，
恢复原 Compose、.env 及迁移前 data，再用本地旧镜像启动。
不删除数据卷，不使用 down -v，不清理旧镜像。

回滚不完整时保留 `.upgrade-lock` 阻止再次升级，要求人工核对；不能直接删锁重试。
即使脚本完成恢复，也必须检查旧版本运行及数据，不能把“尝试回滚”说成已验收。
断电/SIGKILL 无法执行退出处理；现场需按备份恢复。保留旧镜像与对应备份，
不要将迁移后的数据库直接交给旧版本。

本脚本只检查启动、版本和镜像。真实闲鱼搜索、配置筛选、卖家信息、手机推送、
持久化和长期运行仍需实际验收。升级后的任务会按原启用状态继续工作。
