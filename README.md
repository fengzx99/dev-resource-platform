# 开发资源中心

面向内部开发团队的 Docker 开发环境管理平台，提供用户与项目配额、容器生命周期、镜像登记、固定持久存储、实时资源监控和资源使用历史。

本仓库整理自 **2026.10.21 发布版**的当前管理代码；应用健康接口的基线版本仍为 `1.1.0`。完整离线部署包单独分发，应用源代码和工作镜像入口的指纹见 [SOURCE_SNAPSHOT.json](SOURCE_SNAPSHOT.json)。代码采用 [MIT 许可](LICENSE)。

## 当前功能

| 功能 | 说明 |
| --- | --- |
| 用户与账户 | 管理员、开发者、审计员；管理员创建用户和重置密码；用户通过账户设置修改自己的密码 |
| 项目与配额 | 项目和成员授权；CPU、内存、GPU、环境数配额；可选申请审批 |
| 开发环境 | 镜像、套餐、目标节点、SSH 公钥、有效期；申请、启停、重建、续期、删除 |
| GPU 访问 | 支持独占或共享整卡访问；共享访问不保证空闲显存或算力 |
| 镜像目录 | 登记已有镜像、封装自定义基础镜像、启停与项目授权；管理员可移除目录登记 |
| 固定挂载 | 新环境固定 `/data`、`/root`、`/share`；按机器 IP 选择私有数据父目录 |
| 实时监控 | CPU、内存、存储用量；逐卡 GPU 利用率、显存、温度、功耗及进程归属 |
| 资源历史 | 容器资源分配记录、GPU 指标；按用户和项目查询、汇总及 CSV 导出 |
| 节点管理 | Docker 多节点接入、身份与挂载校验、项目授权、维护模式 |
| 运维 | 审计、离线发布、数据库和服务密钥备份、增量数据库建表与字段迁移 |

这是容器开发环境平台，当前未集成 Slurm 或 Ray 作业调度。平台外容器可被资源监控发现，不会被自动纳管、迁移或停止。

## 存储约定

| 容器目录 | 用途 | 宿主机来源 |
| --- | --- | --- |
| `/share` | **存放数据和要读取的模型权重**；全平台共同读写 | `/mnt/juicefs/share` |
| `/root` | **用户主目录、配置、个人开发环境等** | `<节点父目录>/user_home/<环境名-ID>` |
| `/data` | **模型权重输出位置** | `<节点父目录>/user_data/<环境名-ID>` |

**`/data` 和 `/root` 的硬盘空间有限，使用超过 700GB 请及时清理。** 700GB 是管理提醒，目前不是强制目录配额或自动清理阈值。界面的“弹性”仍受底层磁盘可用容量限制。

机器 IP 与父目录由 [app/storage.py](app/storage.py) 的 `IP_ROOTS` 决定：

| IP | 父目录 |
| --- | --- |
| `172.16.9.10` | `/mnt/server001` |
| `172.16.9.11` | `/mnt/server002` |
| `172.16.9.20` | `/mnt/server003` |
| `172.16.9.21` | `/mnt/server004` |

父目录需实际挂载到相应节点的 `/data-local`，同名空目录不会被当成有效存储。主项目必须位于实际挂载的 JuiceFS 上。私有数据目录不会自动跨节点移动；`/share` 通过共同的 JuiceFS 挂载共享。旧式卷的兼容代码保留，新建环境不再手工选择任意卷和挂载路径。

## 使用流程

1. 管理员建立用户与项目，设置成员配额、镜像授权和资源套餐，确认目标节点在线且接收申请。
2. 开发者申请环境，选择项目、节点、镜像、套餐、GPU 使用方式，填写个人 SSH 公钥和有效期。
3. 平台自动绑定三类持久目录，检查权限、配额、节点和存储身份后创建容器；资源不足时等待。
4. 使用环境详情中的 SSH 命令，以 **root 公钥登录**，也可使用页面命令执行台。网页登录密码与 SSH 私钥分别管理。
5. 输入数据和模型权重放在 `/share`；个人配置与开发环境放在 `/root`；输出权重放在 `/data`。
6. 在实时监控和资源历史中检查资源占用。停止或到期会移除计算容器并结束其中进程，固定目录文件保留。

续期按“当前时间 + 申请小时数”重设到期时间。删除环境仍保留固定目录；需要运维根据实际数据和归档策略清理。容器重建后连接端口可能改变，应读取环境详情中的最新地址。

## 监控与权限

管理员可查看节点全量 GPU 和进程。开发者只可查看本人有访问权、且有实际运行环境分配到的 GPU；共享卡展示该卡上的全部进程，包括其他用户及平台外容器的任务。没有实际分配卡的环境不获得其他 GPU 的进程访问权。CPU、内存和存储为可见节点的整体统计，环境汇总只列出本人环境。审计员不开放实时监控。

实时页面约每 5 秒刷新；节点采样有短时缓存。资源历史约每 60 秒采样一次，指标保留 30 天，单次查询时间范围最长 31 天；分配记录长期保留。共享卡的总体利用率不代表某个用户独占使用的算力。采样历史不保存进程启动命令。

管理员重置密码会使目标用户的网页登录会话失效，不停止其开发容器。停用用户或调整角色可能触发其环境停止。镜像移除为目录软移除，底层 Docker 镜像及数据保留；仍被环境引用时会拒绝移除。

## 代码结构

```text
app/
  main.py          API、权限校验与环境控制循环
  auth.py          Keycloak OIDC、会话与账号管理
  models.py        PostgreSQL 数据模型
  policy.py        配额和挂载规则
  storage.py       固定目录与 IP 映射
  nodes.py         节点路由、身份验证与增量迁移
  executor.py      Docker、GPU 和存储适配
  monitoring.py    节点与 GPU 进程采样
  history.py       分配历史、指标采集与导出
  static/          中文前端
workspace/         root 开发镜像 Dockerfile 与 SSH 入口
scripts/           当前维护的部署、备份与发布工具
release/           离线安装器、完整性校验器、部署文档模板
tests/             Python 回归测试与前端 DOM 测试
THIRD_PARTY_LICENSES/  第三方依赖许可
```

仓库不包含服务密钥、站点密码、数据库、用户文件、SSH 私钥、生产验收记录、Docker 镜像和离线 wheel。历史迁移与现场验收脚本仍保留在原工作目录中，但不纳入本仓库；它们不是新版本的通用安装入口。

## 部署

完整步骤见 [部署文档](release/部署文档.html)。当前默认架构：

```text
浏览器 HTTPS :8420
        |
   drp-portal ───── drp-keycloak（OIDC）
        |                 |
        +────── drp-database（PostgreSQL）
        |
   drp-executor（内部接口）
        |
   Docker Engine ── root 开发容器
```

前提为 Linux amd64、Docker、NVIDIA 驱动及 Container Toolkit、正确的 JuiceFS 和固定存储挂载。离线包提供平台、工作环境和基础服务镜像，以及 Python 3.12 wheel；宿主机基础设施需单独准备。

**全新安装应使用校验过的完整离线包。** 包内 `manifest.json` 和校验和是在打包时生成的，不存放在本代码仓库。将完整包解压后执行：

```bash
sha256sum -c SHA256SUMS
python3 verify.py

# 默认只检查；目标目录必须尚不存在
python3 deploy.py --host <本机IPv4> \
  --target /mnt/juicefs/software/dev-resource-platform

# 检查通过后执行全新安装
python3 deploy.py --host <本机IPv4> \
  --target /mnt/juicefs/software/dev-resource-platform --install
```

安装生成随机的 admin/developer 初始密码，保存在目标目录 `runtime/secrets/initial-access.txt`。该文件只记录初始化时的密码，后续修改密码不会更新它。安装会初始化一个项目、两个套餐和一个 root 开发镜像，不自动创建开发容器。

已有站点使用部署文档中的备份与定向升级流程。`deploy.py --install` 拒绝覆盖现有目标目录；不要用重新初始化账号代替升级。`scripts/bootstrap.py` 是底层启动工具，可能替换服务容器，不应直接在现网作为无影响的重装命令执行。

## 本地开发与测试

前端 DOM 测试使用 Node.js 和锁定的 jsdom 依赖，运行平台本身不需要 Node.js：

```bash
npm ci --ignore-scripts
npm test
```

Python 运行环境为 3.12。普通开发环境可建立虚拟环境安装锁定依赖后执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -q
```

离线开发时从发布包解压 wheel 到 `vendor/wheels`，安装命令改为 `pip install --no-index --find-links=vendor/wheels -r requirements.txt`。wheel 与运行平台均面向 Linux amd64，不适用于在 macOS 主机直接安装平台二进制依赖。

从源码离线构建前，先导入发布包基础镜像，并准备 wheel：

```bash
# 在本仓库根目录执行；发布包 wheel 的第一级目录为 dev-resource-platform
# 以下归档路径替换为实际位置
tar -xzf /path/python-wheels-cp312-linux-amd64.tar.gz \
  --strip-components=1 -C .
docker load -i /path/images-linux-amd64.tar.gz

docker build --network=none -t dev-resources/platform:dev .
docker build --network=none -t dev-resources/workspace:dev workspace

docker run --rm --network none --entrypoint python \
  dev-resources/platform:dev -m pytest -q
```

回归测试覆盖权限、配额、JWT、固定挂载、共享 GPU、实时监控可见范围、历史统计、镜像移除与账户设置。Python 测试不需要生产数据库或 GPU；不要将历史现场验收脚本当成单元测试运行。

## 备份与发布工具

| 入口 | 用途 |
| --- | --- |
| `scripts/bootstrap.py` | 构建与基础服务启动；支持指定镜像、实例前缀和端口 |
| `scripts/deploy_node.py` | 在已配置的计算节点部署 TLS 执行器，然后由管理员登记 |
| `scripts/backup.py` | 备份 PostgreSQL 与服务密钥；可在临时库中验证恢复 |
| `scripts/history_backup.py` | 定向更新前保存运行容器、账号/密钥指纹与数据库快照 |
| `scripts/upgrade_history.py` | 兼容执行器的现有站点仅替换门户，失败时恢复旧门户 |
| `scripts/upgrade_monitoring.py` | 已授权维护窗口内更新门户和执行器并保留回滚容器 |
| `scripts/package_release.py` | 制作带版本、镜像清单与校验和的离线发布包 |
| `scripts/initialize_release.py` | 对全新空站点初始化项目、套餐与镜像目录 |
| `release/verify.py` | 校验离线包文件、归档结构、Docker/OCI 镜像身份和 root 标签 |
| `release/deploy.py` | 受保护的全新安装入口，需在完整离线包内使用 |

打包工具需要已部署站点、对应镜像及 `vendor/wheels`，并核对当前门户中的应用代码。示例：

```bash
python3 scripts/package_release.py --version <新发行版号> \
  --platform-image <当前平台镜像引用> \
  --workspace-image <当前root工作镜像引用>
```

数据库与密钥备份不包含工作文件。还需单独备份 `data/.cluster-id`、保留的旧式卷、每个节点的 `user_data` / `user_home`、`/mnt/juicefs/share` 和扩展节点的本机连接配置。恢复现有账号需要匹配的数据库与服务密钥，不能仅靠源码或离线安装包恢复。

## 运行边界

- 管理服务为单实例，当前没有高可用、自动跨节点迁移或多节点合并算力。
- 容器共享宿主机内核；执行器是持有 Docker socket 的可信管理组件。
- GPU 共享不等于显存或算力隔离；平台外负载仍可能竞争 CPU、内存和 GPU。
- 节点失联时保留未知状态资源，不提前释放配额或静默迁移私有目录。
- 实机部署验收不能由模拟节点测试替代；新增物理节点需分别验证身份、挂载、GPU 和连接。
