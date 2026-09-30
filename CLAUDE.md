# 简历分析工具

Python 命令行工具，目标：**给一份 JD 和一批简历，告诉 HR 先看谁、为什么**。定位：中小企业校招季的 HR 助手。读取简历（PDF / Word / TXT / 图片），调用 DeepSeek 提取信息，按规则和知识库分模块给出描述性分析报告（不给总分），再按岗位要求生成阅读优先队列。作者是求职中的学生，这是作品集项目，目前只专注校招。

**开始工作前先读 `docs/开发说明.md`**：规则、关键决定、已知局限和进度（公开版，只用虚构例子）。本地如有 `docs/设计记录.md`（更详细的讨论过程，含化名例子，不上传）可一并参考；新定的规则两份都要同步。

## 工作方式（和用户约定好的）

- **先讨论思路和规则，用户确认后再写代码**；用户常要求多轮"深度思考"，每一轮要找出上一版的根本问题，而不是堆功能；几轮后主动建议收敛、用执行来检验
- 规则和阈值由用户决定；给建议时要给出理由，用 `samples/` 里的虚构简历举例
- 涉及 3 个以上文件的改动，先给中文实施方案，确认后再写
- 做完一块，用真实简历逐条对照原文核对，发现问题如实说明；改提示词后不用缓存跑两遍对比稳定性
- 新确定的规则、决定要同步写进 `docs/开发说明.md`（本地另有设计记录也同步）
- 性能：先打通链路，再整体优化（见开发说明）

## 核心原则

1. AI 只负责提取事实，所有判断由代码规则 + 知识库完成；能用代码判断的不交给 AI（AI 每次结果不同）
2. 不推测：没写的就是没写；推断、默认值必须标注并提示 HR 核实
3. AI 填写的关键字段要用代码核对原文，找不到对应字样就视为没写
4. 以**证据**为核心：别人的决定 > 可核验的具体行为 > 自述；输出证据强度，不输出"素质高低"
5. 宁可多留，不可错杀：未体现 ≠ 不符合，进"待确认"；不确定的事实碰到门槛也进"待确认"
6. 合规：性别、年龄、民族、籍贯、婚育、政治面貌不作为筛选条件；只爬允许的公开来源，不绕过验证

## 隐私

- **真实简历已于 2026-10-01 按用户要求永久删除，以后不再放进项目**（隐去个人信息也不行）；验证、演示只用 `samples/` 的虚构简历
- 简历分析缓存在 `.cache/resumes/`，30 天自动删除，网页侧边栏可一键清除；JD 缓存在 `.cache/jd/`，长期保留
- 本地的设计记录、预期结果是内部讨论记录，在 .gitignore 里，不上传；公开文档只用虚构例子
- API Key 是用户私密信息，不要显示或修改；`.env` 优先于系统环境变量

## 目录结构

```
app.py                  网页版（streamlit run app.py）：①岗位要求（可选）②上传简历 ③总览排序 / 阅读队列；只做展示，判断复用命令行代码；
                        PUBLIC_DEMO=1 为公开部署模式（只用访客的 Key、不写硬盘）；"看演示"读 demo/
main.py                 简历分析入口；MODULES 列表里注册模块；run_modules() 返回模块结果和候选人画像
screen.py               筛选入口：导入 JD、检查 HR 改过的岗位要求、筛选（分队列 + 理由 + CSV）
setup_key.py            API Key 配置窗口（写入 .env）；网页侧边栏也能填写
parser/                 file_reader.py 读取文件；ocr.py 图片识别；privacy.py 脱敏
llm/client.py           DeepSeek 调用；结果缓存在 .cache/；同一提示词 + 同一简历同时只调用一次
modules/                base.py 接口（ModuleResult 带 profile）；basic_info、education、honors（含科研成果）、
                        experience（实习 / 校园共用提取）、internship（含全职工作）、campus、project_skill、quality（素质画像）
matching/               profile.py 合并候选人画像（证据列表）；jd_extract.py JD 提取（知识库和企业导入共用）；
                        jd.py 企业导入 JD 的解析、职责提取、参考岗位、推荐最看重 3 项、HR 文字文件读写；
                        match.py 职责匹配、能力证据分级、队列、理由、电话问题；
                        overview.py 不需要 JD 的总览分层（9 个维度、全面不差才分先后、硬性要求筛选）
knowledge/              schools / rankings / companies / private500 / top_firms / majors / competitions / regions /
                        skill_domains（能力词典）/ quality_signals / job_categories（28 个岗位及爬取关键词）/
                        job_profiles（通用岗位画像）/ common_words（JD 里各词的出现比例，判断泛词）.json（后两个由脚本生成）；kb.py
report/formatter.py     文字报告（末尾有合规声明）；screening.py 筛选结果的理由、队列报告、CSV（命令行和网页共用）
tools/                  update_rankings.py、update_companies.py 每年更新；crawl_jd.py 从 24365 爬校招 JD；
                        build_job_profiles.py 统计通用岗位画像
jobs/                   导入 JD 后生成的岗位要求文件
data/                   爬到的原始 JD（不上传）
tests/                  网页回归测试（Playwright + Edge，公开模式启动，不需要 Key）
samples/                虚构样例；samples/test/ 虚构测试集（dev 开发集、holdout 检验集）；samples/jd/ 测试用 JD（24365 公开岗位）
docs/                   开发说明.md（公开：规则、决定、进度）；设计记录.md、预期结果.md（仅本地，不上传）
```

## 常用命令

```
streamlit run app.py                          # 打开网页版（http://localhost:8501）
python main.py samples/                       # 分析虚构样例简历
python main.py 简历.pdf --show-extracted       # 同时输出 AI 提取的原始数据
python main.py samples/ --no-cache            # 不用缓存，重新调用 AI
python screen.py 导入 samples/jd/数据分析工程师.txt   # 解析 JD，生成 jobs/xxx.json（原始理解）和 jobs/xxx.txt（HR 改这个）
python screen.py 检查 jobs/数据分析工程师.txt         # 读取 HR 改过的文件，显示理解和看不懂的行
python screen.py 筛选 jobs/数据分析工程师.txt samples samples/test/dev   # 分队列、写理由，生成 jobs/xxx_筛选结果.csv
python screen.py 总览 samples samples/test/dev --排序 实习经历,项目经历,技能   # 不需要 JD 的总览分层
python tools/build_demo.py                    # 重新生成"看演示"用的虚构简历结果（规则改了以后要跑）
python tools/crawl_jd.py [--only 岗位]         # 爬取校招 JD（有缓存，可断点续爬）
python tools/build_job_profiles.py            # 用 flash 提取 JD 并统计岗位画像（有缓存）
python setup_key.py                            # 配置 API Key 和模型
python tests/test_job_form.py                 # 网页岗位要求表单回归测试（Edge 无头；先 pip install -r requirements-dev.txt）
python tools/update_rankings.py / update_companies.py   # 每年更新排名、公司名单
```

## 注意

- 改了提示词会导致缓存失效、重新调用 AI；对比新旧结果时注意这一点
- `matching/jd_extract.py` 的提示词和知识库构建共用，改动会让 1600 份 JD 的缓存失效
- **不要一直打补丁去迎合简历**：规则和词典来自 JD 库等通用数据；结果对不上先分通用问题 / 个别情况，个别情况记入 `docs/预期结果.md`
- 给 HR 看的文字不用内部术语（L1～L3、"基础"档等），用直白说法

## 语言（用户明确要求，每次回复都要遵守）

- **始终用简体中文回复**：所有回复、解释、总结、提问都用中文，不夹英文说明；界面上的英文按钮、菜单要引用时，旁边注明中文意思（如"点 Reboot（重启）"）
- 代码、命令、文件名、报错信息，以及技术术语和格式名（PDF、Word、Excel、API 等）保持原文，不翻译；其余解释用中文
- git commit message 用英文；其余（文档、界面文字、PR 说明）用中文
