# 评委完整一键运行说明

> 使用提示：本包包含已获准用于竞赛展示的模拟/受控数据与知识资源，仅用于大赛评审和技术复现，不代表真实生产数据。

本包已经内置成本CSV、行业参考、知识文档、Word报告模板、模拟RPA接口资料和知识证据索引。评委无需再复制或配置数据目录。

## 一、推荐启动方式：Docker

适用于 Windows、macOS 和 Linux，需要提前安装 Docker Desktop 或 Docker Engine（含 Compose v2）。

### Windows

双击：

```text
启动跨环境演示.cmd
```

启动脚本已固定 Docker Compose 项目名为 `pharma-cost-intelligence`，解压目录可使用中文、空格或下划线；目录名不会参与镜像标签生成。

### macOS / Linux

在解压目录执行：

```bash
bash start-docker.sh
```

### 通用命令

```bash
docker compose -p pharma-cost-intelligence up --build -d
```

首次构建完成后访问：

```text
http://127.0.0.1:8080
```

停止服务：

```bash
docker compose -p pharma-cost-intelligence down
```

容器启动前会自动检查10份CSV、知识索引、107字段报告契约、Word模板和LibreOffice；关键文件缺失时将停止启动并给出错误原因。

## 二、Windows原生启动：备用方式

若电脑已经安装 Python 3.11+ 和 LibreOffice，可直接双击：

```text
一键启动系统.cmd
```

请先将 ZIP 完整解压，不要在压缩软件内直接运行 CMD。建议安装 Python 3.12（勾选 Add Python to PATH）及 LibreOffice。启动器会依次寻找 `py`、`python`、`python3`，验证 Python 3.11+，再为当前目录建立 `.venv` 并联网安装依赖。不需要安装在 D 盘或使用参赛者的用户名。

若检测到旧电脑或移动目录后的虚拟环境，启动器会将它保留为 `.venv.backup-*` 并自动重建；依赖缺失时会重新安装。系统环境缺失时会给出安装提示，不会自动安装系统软件。后续在同一目录启动可复用有效环境。路径支持中文和空格。默认端口被占用时会自动选择下一组可用端口，请以输出的 Dashboard 地址为准。

没有 Python/LibreOffice 时，可安装并启动 Docker Desktop，改用“启动跨环境演示.cmd”。两个入口都需要先满足上述运行前提，并非零依赖离线启动。Qwen 密钥需要在评委电脑上单独配置，不会随压缩包迁移。

停止时双击：

```text
停止演示系统.cmd
```

## 三、通义千问说明

未配置API Key时，确定性成本分析、网页看板、Word/PDF报告和整改任务候选仍可运行，大模型叙述会使用安全回退文本。

如需测试通义千问：

1. Docker方式：将 `.env.example` 复制为 `.env`，填写评审方自己的 `COST_LLM_API_KEY`，然后重新启动容器；
2. Windows原生方式：运行 `配置通义千问API.cmd`，密钥仅由当前Windows用户加密保存。

本包不含参赛团队的真实API Key。

## 四、完整演示流程

1. 选择月度成本分析、产品和月份；
2. 点击“运行分析”，查看经营概览、趋势图、成本热力图、人工分析和差异归因；
3. 点击“生成报告”，在线预览或下载Word/PDF；
4. 在差异归因模块查看差异总览、结构树、归因文本和改进建议；
5. 进入整改闭环，为候选任务填写责任人和截止时间，人工确认后模拟发送RPA；
6. 查看任务状态和模拟送达回执。

## 五、包内关键目录

```text
app/                    系统源码
scripts/                启动、预检与评测脚本
tests/                  自动化测试
docs/                   技术方案和评测说明
01_成本明细数据/        成本及预算数据
02_行业参考数据/        市场行情和行业基准
03_制药知识文档/        RAG知识来源
04_报告模板/            Word/Markdown模板
05_RPA接口文档/         赛题模拟接口资料
06_知识证据索引/        已构建的受控检索索引
```

`JUDGE_PACKAGE_MANIFEST.json`记录每个文件的大小和SHA-256，可用于提交前后完整性核验。

正式交付文档以包内以下四份文件为准：

- `（创灵境）成本智能分析系统技术方案文档.docx`
- `（创灵境）成本智能分析系统技术方案文档.pdf`
- `（创灵境）成本智能分析系统竞赛评测报告.docx`
- `（创灵境）成本智能分析系统竞赛评测报告.pdf`

## 六、验收命令

```bash
python -m unittest discover -s tests
python scripts/competition_evaluation.py --project-root . --execute-rpa
```

2026-09-03最终代码基线：213项自动化测试通过，2项因未安装可选`llama-index-core`而跳过，0失败。端到端结果以本次发布包内清单及验收记录为准。Docker文件已经完成静态检查；如目标机采用Docker，首次实际镜像构建仍应在其本机完成。
