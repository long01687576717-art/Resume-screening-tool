"""知识库：加载院校、专业、地区数据，并提供查询。"""
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).parent

# 各档位的层级（数字越小层次越高），用于比较不同阶段的院校层次。
# 海外 QS 区间与国内档位同级比较；中外合作办学、待定、未分档不参与比较。
TIER_LEVELS = {
    "顶尖院校": 0, "海外 QS 前 50": 0,
    "985 院校": 1, "海外 QS 前 100": 1,
    "211 院校": 2, "双一流院校": 2, "海外 QS 前 200": 2,
    "双非第一梯队": 3, "双非第二梯队": 4, "双非第三梯队": 5,
    "双非第四梯队": 6, "双非第五梯队": 7, "双非第六梯队": 8,
    "基础": 9,
}

# 学校名后面紧跟"XX学院"时，用来区分是院系（如"计算机学院"）还是独立学院（如"锦城学院"）
DEPARTMENT_WORDS = re.compile(
    r"计算机|软件|信息|电子|电气|通信|机械|自动化|经济|管理|商|法|数学|物理|化学|生命|生物|医|药|护理|"
    r"外国语|外语|新闻|传媒|传播|艺术|设计|建筑|土木|材料|能源|环境|化工|统计|公共|马克思|教育|体育|"
    r"音乐|美术|历史|哲学|社会|国际|光电|航空|航天|交通|船舶|海洋|地球|资源|农|林|食品|研究生|"
    r"工程|科学|技术|人文|政治|金融|会计|数据|智能|集成电路|仪器|力学|测绘|规划|旅游|文|理|工"
)
ADULT_EDU_WORDS = re.compile(r"继续教育|网络教育|成人教育|远程教育")

# 公司名称规范化：去掉括号里的地区、合伙形式，去掉开头的省市和结尾的"股份有限公司"等
COMPANY_BRACKET = re.compile(r"（[^）]*）")
COMPANY_SUFFIX = re.compile(r"(集团)?(控股)?(股份)?(有限责任公司|有限公司|公司)$|集团$")
REGION_SUFFIX = r"(省|市|自治区|壮族自治区|回族自治区|维吾尔自治区|特别行政区)?"
# 机关、事业单位（名称里没有"公司"时才判断）
PUBLIC_ORG = re.compile(r"人民政府|政府|街道|委员会|工委|团委|法院|检察院|(?<![工程一二三四五六七八九十\d])局|厅|海关|医院|学校|大学|学院|研究所|研究院|电视台|博物馆|图书馆|中心")
# 分支机构："XX银行上海分行""XX保险北京分公司"，去掉后按总公司匹配
BRANCH_UNITS = r"(?:分行|支行|分公司|营业部|分所|办事处|代表处)$"
# 集团的省级公司："中国移动通信集团广东有限公司"→"中国移动通信集团有限公司"
PROVINCIAL_UNIT = r"集团(?:{places}){region}有限公司$"
# 常见简称前缀
PREFIX_ALIAS = {"国网": "国家电网有限公司"}
# 简称去掉这些结尾后作为品牌词，用来提示"疑似子公司"
STEM_SUFFIX = re.compile(r"(股份|集团|控股|科技|实业)+$")
COMPANY_WORDS = re.compile(r"公司|事务所|银行|集团")


@dataclass
class CompanyMatch:
    tier: str         # 头部平台 / 知名平台 / 一般平台 / 机关 / 事业单位
    basis: str        # 定档依据，如"世界 500 强""A 股上市""互联网头部"
    listed: bool      # 是否在名单中找到
    parent: str = ""  # 疑似母公司（未在名单中找到，但名称以名单内公司的品牌开头）


@dataclass
class SchoolMatch:
    name: str             # 识别出的学校名（含分校区）
    record: dict | None   # 知识库记录，未收录时为 None
    city: str             # 所在城市（海外院校为国家/地区）
    independent: bool = False  # 疑似独立学院
    adult_edu: bool = False    # 继续教育 / 网络教育学院


def _load(name):
    with open(DATA_DIR / name, encoding="utf-8") as f:
        data = json.load(f)
    data.pop("说明", None)
    return data


def _competition_key(name):
    """竞赛名称规范化：去掉年份、届数、引号、书名号、空格和标点，统一大小写。"""
    text = re.sub(r"(19|20)\d{2}\s*年?", "", name)
    text = re.sub(r"第[一二三四五六七八九十百零\d]+届", "", text)
    text = re.sub(r"[“”\"'‘’《》「」（）()\s·\-—_、,，.。]", "", text)
    return text.upper()


def normalize(text):
    """统一全角/半角括号和空白，方便匹配。"""
    return re.sub(r"\s+", " ", text.replace("(", "（").replace(")", "）")).strip()


class KnowledgeBase:
    def __init__(self):
        self._school_patterns = []  # (匹配文字长度, 正则, 记录)
        self._majors = []           # (专业名, 门类, 专业类)
        self._regions = _load("regions.json")
        self._load_schools()
        self._load_rankings()
        self._load_majors()
        self._competitions = []  # (规范化后的名称, 记录)，长名优先
        self._load_competitions()
        places = sorted({p for province, cities in self._regions.items() for p in [province, *cities]}, key=len, reverse=True)
        self._region_prefix = re.compile(rf"^(?:(?:{'|'.join(map(re.escape, places))}){REGION_SUFFIX})+")
        self._top_firms = []    # (正则, 排除词, 机构名, 行业)
        self._branch = self._provincial = None
        self._company_cores = {}  # 核心名称 → 定档依据，按优先级先写入的不覆盖
        self._unicorns = []       # (匹配方式, 品牌名)
        self._stems = {}          # 品牌词 → 公司名，用于提示疑似子公司
        self._load_companies()
        self._load_skill_domains()

    # ---------- 院校 ----------

    def _load_schools(self):
        data = _load("schools.json")
        for rec in data["domestic"]:
            rec["domestic"] = True
            tags = rec["tags"]
            rec["tier"] = rec.get("tier") or ("985 院校" if "985" in tags else "211 院校" if "211" in tags else "双一流院校")
            rec["label"] = tags[0] if rec["tier"] == "顶尖院校" else ""  # 其他档位名称已包含身份，不重复
            self._add_school(rec)
        for rec in data["overseas"]:
            rec["domestic"] = False
            rec["city"] = rec["country"]
            rec["tier"] = f"海外 QS 前 {rec['qs']}"
            rec["label"] = rec["country"]
            self._add_school(rec)
        # 长名字优先，避免"电子科技大学"抢先匹配"西安电子科技大学"
        self._school_patterns.sort(key=lambda p: -p[0])

    def _load_rankings(self):
        """软科排名梯队（由 tools/update_rankings.py 生成）；与 schools.json 重名的以 schools.json 为准。"""
        path = DATA_DIR / "rankings.json"
        if not path.exists():
            return
        known = {rec["name"] for _, _, rec in self._school_patterns}
        for name, info in _load("rankings.json")["schools"].items():
            name = normalize(name)
            if name not in known:
                self._add_school({"name": name, "domestic": True, "city": "", "tier": info["tier"], "label": info["label"]})
        self._school_patterns.sort(key=lambda p: -p[0])

    @staticmethod
    def tier_level(tier, record=None):
        """档位的层级（数字越小层次越高）；记录里单独指定了 level 时以它为准。无法比较时返回 None。"""
        if record and "level" in record:
            return record["level"]
        return TIER_LEVELS.get(tier)

    def _add_school(self, rec):
        for name in [rec["name"], *rec.get("alias", [])]:
            name = normalize(name)
            if name.isascii():
                # 英文名要求前后不是字母，避免"HKU"匹配到"HKUST"；全大写缩写区分大小写
                flags = 0 if name.isupper() else re.IGNORECASE
                regex = re.compile(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", flags)
            else:
                regex = re.compile(re.escape(name))
            self._school_patterns.append((len(name), regex, rec))

    def match_school(self, name):
        """根据学校名称查找知识库记录。找不到时返回 record=None 的结果。"""
        text = normalize(name or "")
        if not text:
            return None
        for _, regex, rec in self._school_patterns:
            m = regex.search(text)
            if m:
                return self._build_match(text, m, rec)
        return SchoolMatch(name=text, record=None, city="", **self._suffix_flags(text, len(text)))

    def _build_match(self, text, m, rec):
        name, city = rec["name"], rec.get("city", "")
        after = text[m.end():]
        # 分校区，如"哈尔滨工业大学（深圳）""山东大学（威海）"
        branch = re.match(r"（([一-龥]{2,6}?)(?:校区)?）", after)
        if branch and not rec["name"].endswith("）"):
            name = f"{rec['name']}（{branch.group(1)}）"
            city = branch.group(1)
            after = after[branch.end():]
        flags = self._suffix_flags(after, 0)
        if flags["independent"]:
            # 独立学院和母校是两所学校，保留全名，不沿用母校的城市
            college = re.match(r"[一-龥]{2,6}学院", after).group()
            return SchoolMatch(name=name + college, record=rec, city="", **flags)
        return SchoolMatch(name=name, record=rec, city=city, **flags)

    @staticmethod
    def _suffix_flags(text, start):
        """检查学校名后面紧跟的"XX学院"：独立学院 / 继续教育学院。"""
        m = re.match(r"([一-龥]{2,6})学院", text[start:])
        if not m:
            return {"independent": False, "adult_edu": False}
        word = m.group(1)
        if ADULT_EDU_WORDS.search(word):
            return {"independent": False, "adult_edu": True}
        return {"independent": not DEPARTMENT_WORDS.search(word), "adult_edu": False}

    # ---------- 专业 ----------

    def _load_majors(self):
        for discipline, classes in _load("majors.json").items():
            for major_class, majors in classes.items():
                for major in majors:
                    self._majors.append((major, discipline, major_class))
        self._majors.sort(key=lambda m: -len(m[0]))

    def match_major(self, name):
        """返回 (门类, 专业类)；先精确匹配，再找包含关系（长名优先）。"""
        text = normalize(name or "").replace("专业", "")
        if not text:
            return None
        for major, discipline, major_class in self._majors:
            if text == major:
                return discipline, major_class
        for major, discipline, major_class in self._majors:
            if major in text:
                return discipline, major_class
        return None

    # ---------- 竞赛目录 ----------

    def _load_competitions(self):
        for rec in _load("competitions.json")["competitions"]:
            rec.setdefault("type", "本科")
            for name in [rec["name"], *rec["alias"]]:
                self._competitions.append((_competition_key(name), rec))
        self._competitions.sort(key=lambda c: -len(c[0]))

    def match_competition(self, name):
        """在《全国普通高校大学生竞赛分析报告》竞赛目录中查找，找不到返回 None。
        先比较规范化后的全名/简称，再看是否互相包含（长名优先，较短的一方至少 3 个字，避免"电赛"这类短简称误配）。"""
        key = _competition_key(name or "")
        if not key:
            return None
        for known, rec in self._competitions:
            if key == known:
                return rec
        for known, rec in self._competitions:
            shorter = min(len(known), len(key))
            if shorter >= 3 and (known in key or key in known):
                return rec
        return None

    # ---------- 公司 ----------

    def _load_companies(self):
        for industry, group in _load("top_firms.json")["industries"].items():
            for firm in group["firms"]:
                words = []
                for word in firm["keywords"]:
                    # 英文关键词要求前后不是字母，避免"EY"匹配到"KEY"
                    words.append(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])" if word.isascii() else re.escape(word))
                regex = re.compile("|".join(words), re.IGNORECASE)
                self._top_firms.append((regex, firm.get("exclude", []), firm["name"], industry, group["tier"]))
        path = DATA_DIR / "companies.json"
        if not path.exists():
            return
        data = _load("companies.json")
        data["private500"] = _load("private500.json")["companies"]
        # 优先级：世界 500 强 > 中国 500 强 > 民企 500 强 > A 股上市，同一家公司取最高的
        for key, tier, basis in (("global500", "头部平台", "世界 500 强"), ("china500", "知名平台", "中国 500 强"),
                                 ("private500", "知名平台", "民营企业 500 强"), ("listed", "知名平台", "A 股上市")):
            for company in data[key]:
                names = [company] if isinstance(company, str) else [company["name"], *company["alias"]]
                # 全称已在更高的名单里时，简称沿用同一档（"工商银行"和"中国工商银行"都是世界 500 强）
                known = self._company_cores.get(self._company_core(names[0]), (tier, basis))
                for name in names:
                    core = self._company_core(name)
                    self._company_cores.setdefault(core, known)
                    stem = STEM_SUFFIX.sub("", core)
                    if len(stem) >= 2 and not stem.isascii():
                        self._stems.setdefault(stem, names[0])
        # 独角兽榜只有品牌名（如"大疆""米哈游"），按品牌匹配：3 个字以上的包含即可，2 个字的要在名称开头
        for brand in data.get("unicorns", []):
            if brand.isascii():
                self._unicorns.append((re.compile(rf"(?<![A-Za-z]){re.escape(brand)}(?![A-Za-z])", re.IGNORECASE), brand))
            else:
                self._unicorns.append((brand, brand))
        places = "|".join(sorted({p for province, cities in self._regions.items() for p in [province, *cities]}, key=len, reverse=True))
        self._branch = re.compile(rf"(?:股份有限公司|有限责任公司|有限公司)?(?:{places}){REGION_SUFFIX}{BRANCH_UNITS}")
        self._provincial = re.compile(PROVINCIAL_UNIT.format(places=places, region=REGION_SUFFIX))

    def _region_of(self, name):
        m = self._region_prefix.match(normalize(name))
        return re.sub(REGION_SUFFIX + "$", "", m.group()) if m else ""

    def _company_core(self, name):
        """公司名的核心部分，用于比较全称和简称："上海晨光文具股份有限公司" → "晨光文具"。"""
        text = COMPANY_BRACKET.sub("", normalize(name).replace(" ", "")).upper()
        text = COMPANY_SUFFIX.sub("", text)
        core = self._region_prefix.sub("", text)
        return core if len(core) >= 3 else text  # 去掉地区后太短（如"上海银行"→"银行"）时保留原样

    def match_company(self, name):
        """按头部机构名单、500 强、上市公司、独角兽名单给公司定档；都找不到时区分机关事业单位和一般企业。
        分支机构（分行、分公司）和集团的省级公司按总公司匹配；子公司只提示"疑似"，不定档。"""
        text = unicodedata.normalize("NFKC", normalize(name or "")).replace("(", "（").replace(")", "）")
        if not text:
            return None
        for regex, exclude, firm, industry, tier in self._top_firms:
            # 内资会计师事务所的关键词较短（如"大华"），要求名称里同时有"会计师事务所"
            if industry.endswith("（内资）") and "会计师事务所" not in text:
                continue
            if regex.search(text) and not any(word in text for word in exclude):
                return CompanyMatch(tier, f"{industry}：{firm}", True)
        for prefix, full in PREFIX_ALIAS.items():
            if text.startswith(prefix):
                text = full
        head = self._branch.sub("", text) if self._branch else text
        head = self._provincial.sub("集团有限公司", head) if self._provincial else head
        for candidate in dict.fromkeys([text, head]):
            found = self._company_cores.get(self._company_core(candidate))
            if found:
                return CompanyMatch(found[0], found[1] + ("（按总公司）" if candidate != text else ""), True)
        core = self._company_core(head)
        for pattern, brand in self._unicorns:
            hit = pattern.search(text) if not isinstance(pattern, str) else (
                pattern in text if len(pattern) >= 3 else core.startswith(pattern))
            if hit:
                return CompanyMatch("知名平台", f"独角兽：{brand}", True)
        if not COMPANY_WORDS.search(text) and PUBLIC_ORG.search(text):
            return CompanyMatch("机关 / 事业单位", "", False)
        # 名称以名单内公司的品牌开头（如"晨光科力普"），只提示疑似子公司，不定档
        for size in range(min(len(core), 6), 1, -1):
            parent = self._stems.get(core[:size])
            if not parent or core[:size] == core:
                continue
            # 两个字的品牌容易撞名（"中天华信"≠"中天科技"），要求两边都写了地区且相同
            if size == 2 and not (self._region_of(text) and self._region_of(text) == self._region_of(parent)):
                continue
            return CompanyMatch("一般平台", "未收录于名单", False, parent=parent)
        return CompanyMatch("一般平台", "未收录于名单", False)

    # ---------- 技能领域 ----------

    def _load_skill_domains(self):
        self._skill_words = []  # (关键词长度, 正则, 层, 领域, 是否通用)
        self.skill_domain_names = []
        for layer, domains in _load("skill_domains.json").items():
            for domain, value in domains.items():
                general = isinstance(value, dict) and value.get("general", False)
                words = value["keywords"] if isinstance(value, dict) else value
                self.skill_domain_names.append(domain)
                for word in words:
                    pattern = rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])" if word.isascii() else re.escape(word)
                    self._skill_words.append((len(word), re.compile(pattern, re.IGNORECASE), layer, domain, general))
        self._skill_words.sort(key=lambda w: -w[0])

    def match_skill_domain(self, name):
        """返回 (层, 领域, 是否通用)；取匹配到的最长关键词，找不到返回 None。"""
        for _, regex, layer, domain, general in self._skill_words:
            if regex.search(name or ""):
                return layer, domain, general
        return None

    def match_skill_domains_all(self, name):
        """名称涉及的全部领域，如"物流成本管理"同时属于采购供应链和成本分析。"""
        return {domain for _, regex, _, domain, general in self._skill_words if not general and regex.search(name or "")}

    def domain_word_at(self, text, domain):
        """这段文字里属于该领域的第一个关键词的位置（开始, 结束），找不到返回 None。"""
        spans = [m.span() for _, regex, _, d, _ in self._skill_words if d == domain for m in [regex.search(text or "")] if m]
        return min(spans) if spans else None

    def skill_layer(self, domain):
        """AI 归类的领域属于哪一层；不在知识库里的返回 None。"""
        return next((layer for _, _, layer, d, _ in self._skill_words if d == domain), None)

    # ---------- 地区 ----------

    def province_of(self, place):
        """返回地点所属省份（简称），无法判断时返回 None。"""
        text = normalize(place or "")
        for province, cities in self._regions.items():
            if text.startswith(province):
                return province
        for province, cities in self._regions.items():
            if any(city in text for city in cities):
                return province
        return None
