开发资源中心离线部署包 2026.10.21
应用基线：1.1.0；发布日期：2026-10-21；平台：Linux amd64。

请先打开同目录的《部署文档.html》。此包不含现网账号密码、私钥、数据库及用户文件。
单文件包：先 sha256sum -c dev-resource-platform-2026.10.21-linux-amd64.tar.sha256，
再 tar -xf dev-resource-platform-2026.10.21-linux-amd64.tar，进入解包目录。
文件校验：sha256sum -c SHA256SUMS
完整离线校验：python3 verify.py

全新安装前检查（目标目录必须不存在；先准备 Docker/NVIDIA/JuiceFS 和固定存储挂载）：
  python3 deploy.py --host 172.16.9.20 --target /mnt/juicefs/software/dev-resource-platform
确认前检查通过后执行安装：
  python3 deploy.py --host 172.16.9.20 --target /mnt/juicefs/software/dev-resource-platform --install

已有平台不能直接运行全新安装命令，具体备份、升级和回滚见部署文档。
安装随机生成 admin/developer 密码，保存于目标目录 runtime/secrets/initial-access.txt。
新安装默认只有一个项目、两种套餐、一个 root 开发镜像，不会自动创建开发容器。
/share：数据与输入模型权重；/root：用户主目录/配置/个人开发环境；/data：模型权重输出。
重点：/data 和 /root 硬盘空间有限，使用超过 700GB 请及时清理；不是自动配额或自动清理。

images-linux-amd64.tar.gz：六个版本固定的 Docker 镜像（含基础镜像）
source-2026.10.21.tar.gz：当前应用/构建/测试/部署源代码（不含运行数据和历史临时脚本）
python-wheels-cp312-linux-amd64.tar.gz：锁定的 Python 3.12 离线依赖
manifest.json / SHA256SUMS / PACKAGE-VERIFICATION.json：版本清单、校验和、完整性验证结果
ACCEPTANCE.json / ACCEPTANCE.sha256（若提供）：发布前安装、恢复及现网保护验证结果
