# 开源组件与许可证

本项目自研代码为 MIT。外部依赖仍按各自许可证使用，并不因打包而变成 MIT。

| 组件 | 本节点版本 / 来源 | 许可证 / 官方证据 |
|---|---|---|
| Docker Engine / Moby | 既有 Docker 29.1.3 | [Apache-2.0](https://github.com/moby/moby/blob/master/LICENSE)；当前节点使用 Linux Engine，未使用 Docker Desktop |
| Keycloak | quay.io/keycloak/keycloak:26.7.4 | [Apache-2.0](https://github.com/keycloak/keycloak/blob/main/LICENSE.txt) |
| PostgreSQL | postgres:16-alpine 离线镜像 | [PostgreSQL License](https://www.postgresql.org/about/licence/) |
| JuiceFS Community | 既有 1.4.1 挂载和客户端 | [Apache-2.0](https://github.com/juicedata/juicefs/blob/main/LICENSE) |
| FastAPI / Starlette / SQLAlchemy / Docker Python SDK | requirements.txt 固定版本 | 各 wheel 内随附的 MIT 等许可证，见 THIRD_PARTY_LICENSES |
| Psycopg | 3.3.6（含 binary wheel） | 随附 LGPL-3.0 文本；按 Python 动态依赖使用，未修改库源码 |
| Python 和其他 Python 依赖 | CPython 3.12 Linux amd64；35 个固定依赖 | 元数据清单 `THIRD_PARTY_LICENSES/python-dependencies.json` 与随附许可文件 |
| 开发镜像 | 节点原有 ubuntu_24.04_template:v1 加平台封装 | Ubuntu/Linux/OpenSSH 等各自许可证，保留镜像内许可；基础镜像包含的其他软件仍按其原许可 |
| NVIDIA 驱动和 Container Toolkit | 复用节点现有安装 | 管理系统不购买 vGPU/商业控制台功能；GPU 驱动与 CUDA 等组件有各自许可，不宣称它们都是 MIT/Apache 开源组件 |

离线包用于当前用户内部部署，不包含 Harbor、Coder 商业功能、KubeSphere 企业组件或训练平台。依赖清单是实际 wheel 和镜像清单，不代替未来对外分发时的完整软件物料和许可审查。官方许可证查阅日期：2026-09-30。
