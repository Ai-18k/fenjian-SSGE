
# 引入模块调用
import execjs
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi.requests import Session
import subprocess
import os
from Core.imports import *


HOME_URL = "https://ec.chng.com.cn/channel/home/"

# 并发与重试配置（可按机器/网络情况调整）
PAGE_WORKERS = 4          # 列表页并发数
DETAIL_WORKERS = 8        # 单页详情并发数
MAX_RETRIES = 5           # 请求最大重试次数

# Node.js 路径（自动检测或手动设置）
NODE_CMD = os.environ.get("NODE_PATH", "node")

def resource_path(relative_path):
    """获取资源的绝对路径"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)

    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)  # 上一级目录
    parent_dir1 = os.path.dirname(parent_dir)  # 上一级目录
    # 构建 js 文件夹下的文件路径
    return os.path.join(parent_dir1, 'js', relative_path)


# ═══ 路径配置 ═══
SCRIPT_DIR = Path(__file__).parent.resolve()
RS6_DIR = SCRIPT_DIR / "rs6_solver"      # rsdemo.js + code/ + node_modules/

class RS6Session:
    """维护 ec.chng.com.cn 的 RS6 Cookie 会话"""

    def __init__(self):
        self.sess = Session()
        self.sess.impersonate = "chrome120"
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        self.cookies_ok = False

    def _set_cookie(self, name, value):
        self.sess.cookies.pop(name, None)
        self.sess.cookies.set(name, value, domain="ec.chng.com.cn", path="/")

    def solve_rs6(self):
        """完整 RS6 挑战求解"""
        r = self.sess.get(HOME_URL, timeout=30)
        if r.status_code == 200:
            self.cookies_ok = True
            return True

        if r.status_code != 412:
            print(f"  Unexpected status: {r.status_code}")
            return False

        nsd = re.findall(r'\$_ts\.nsd=(\d+)', r.text)
        cd_val = re.findall(r'\$_ts\.cd="([^"]+)"', r.text)
        meta_val = re.findall(r'<meta[^>]*content="([^"]{20,})"[^>]*r=.m.', r.text)
        vm_src = re.findall(r'<script[^>]*src="(/[^"]+\.js)"[^>]*r=.m.', r.text)

        if not all([nsd, cd_val, meta_val, vm_src]):
            print("  Failed to extract RS6 materials")
            return False

        nsd, cd_val, meta_val, vm_src = nsd[0], cd_val[0], meta_val[0], vm_src[0]

        vm_url = f"https://ec.chng.com.cn{vm_src}" if vm_src.startswith('/') else vm_src
        vr = self.sess.get(vm_url, timeout=15)
        if vr.status_code != 200:
            print(f"  VM download failed: {vr.status_code}")
            return False

        ts_code = f'$_ts=window[\'$_ts\'];if(!$_ts)$_ts={{}};$_ts.nsd={nsd};$_ts.cd="{cd_val}";if($_ts.lcd)$_ts.lcd();'

        with open(resource_path("rsdemo.js"), "r", encoding="utf-8") as f:
            rsdemo = f.read()

        code = rsdemo.replace("'content_code'", json.dumps(meta_val))
        code = code.replace("'ts_code'", ts_code)
        code = code.replace("'functo_code'", "// none")
        code = code.replace("require('./code/ts.js')\nrequire('./code/source.js')", vr.text)

        tmp = RS6_DIR / "_tmp.js"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(code)

        try:
            r = subprocess.run(
                [NODE_CMD, "--stack-size=16384", str(tmp)],
                capture_output=True, text=True, timeout=60, cwd=str(RS6_DIR)
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Node.js not found. Install Node.js or set NODE_PATH env var.\n"
                f"Tried: {NODE_CMD}"
            )

        output = r.stdout + r.stderr
        s6p = re.findall(r'S6J51OuUjLieP=([^;\s]+)', output)

        if not s6p:
            err_lines = [l.strip()[:200] for l in r.stderr.split('\n') if 'Error' in l]
            print(f"  RS6 solve failed:")
            for l in err_lines[:3]:
                print(f"    {l}")
            return False

        self._set_cookie("S6J51OuUjLieP", s6p[0])

        r2 = self.sess.get(HOME_URL, timeout=30)
        if r2.status_code == 200:
            self.cookies_ok = True
            return True

        print(f"  Home verification failed: {r2.status_code}")
        return False

    def ensure_cookies(self):
        if self.cookies_ok:
            return True
        r = self.sess.get(HOME_URL, timeout=30)
        if r.status_code == 200:
            self.cookies_ok = True
            return True
        return self.solve_rs6()


class HnSpider:
    """中国华能"""
    def __init__(self):
        # 日志
        self.log = SpiderSchedulerLogger().get_logger(__name__)
        # 大模型方法
        self.model = BiddingModel()
        # 去重
        self.checker = URLDuplicateChecker(key_prefix="hn_gg_duplicate")
        # 处理HTML
        self.html = HtmlContentCleaner()
        # 对HTML内容进行base64转码处理
        self.decryp_html = DecryptData()

        self.webSource = "中国华能电子商务平台"
        self.mongo = MonData(collection_name="china_huaneng")
        # 初始化监控状态
        self.logo_info = {
            'WasSuccessful': 1,
            'WebsiteError': 1,
            'HasContent': 0,
            'HasNewData': 0,
            'IsValidData': 1,
            'WebName': self.webSource,
        }
        # 监控方法
        self.storage = WebsiteRedisStorage()
        # 页数
        self.num = None
        self.headers = {
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Content-Type': 'application/json',
            'DNT': '1',
            'Origin': 'https://ec.chng.com.cn',
            'Pragma': 'no-cache',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
            'sec-ch-ua': '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
        }
        self.cookies={}
        self.proxy=None
        self._cookie_lock = threading.Lock()
        self._local = threading.local()
        self.url = "https://ec.chng.com.cn/scm-uiaoauth-web/s/business/uiaouth/queryAnnouncementByTitle"
        self.detail_url = 'https://ec.chng.com.cn/scm-uiaoauth-web/s/business/uiaouth/announcementDetail'
        # 用来判断采购方式类型的列表
        self.bid_type_list = ['公开招标','竞争性磋商','竞争性邀请','询价','竞争性谈判','单一来源','其他方式']

    def main_rs_info(self,rs6):
        """解析瑞数信息"""
        html_data = etree.HTML(rs6)
        content = html_data.xpath('//meta[2]/@content')[0]
        ts_js = html_data.xpath('//script[1]/text()')[0]
        jsurl = html_data.xpath('//script[2]/@src')[0]
        return content, ts_js, jsurl


    def execjs_data(self,sess,content, ts_js, jsurl):
        """执行JS获取cookie"""
        urls = "https://ec.chng.com.cn" + jsurl
        with self._cookie_lock:
            response = sess.get(url=urls, headers=self.headers, cookies=self.cookies, proxies=self.proxy)
            if response.status_code == 200:
                with open(resource_path("v1.js"), mode="r", encoding="utf-8") as f:
                    cookie_doc = f.read().replace('content_code', content).replace("'ts_code'", ts_js).replace(
                        "'functo_code'", response.text)
                coo = execjs.compile(cookie_doc).call("rs6")
                cookie = {
                    "S6J51OuUjLieP": coo.split(';')[0].split("=")[-1],
                }
                self.cookies.update(cookie)

    def _sync_rs6_cookies(self, rs6: RS6Session):
        """将 RS6Session 求解后的 cookie 同步到爬虫实例"""
        for name, value in rs6.sess.cookies.items():
            if name == "S6J51OuUjLieP":
                with self._cookie_lock:
                    self.cookies[name] = value

    def _get_sess(self):
        """每个线程独立 Session，避免 curl 句柄并发冲突"""
        if not hasattr(self._local, 'sess'):
            sess = Session()
            sess.impersonate = "chrome120"
            self._local.sess = sess
        return self._local.sess

    def _ensure_session(self):
        """初始化并预热 RS6 会话（只求解一次 cookie）"""
        if self.cookies.get("S6J51OuUjLieP"):
            return self._get_sess()
        rs6 = RS6Session()
        if not rs6.ensure_cookies():
            raise RuntimeError("RS6 cookie 初始化失败")
        self._sync_rs6_cookies(rs6)
        return self._get_sess()

    @staticmethod
    def timestamp_to_ymd(timestamp_ms, format='%y-%m-%d'):
        # 转换为秒级时间戳
        timestamp_s = timestamp_ms / 1000
        # 转换为datetime对象
        dt = datetime.fromtimestamp(timestamp_s)
        # 返回格式化后的日期
        return dt.strftime(format)


    def detail(self, info, originalWebsiteAddress):
        sess = self._get_sess()
        for attempt in range(MAX_RETRIES):
            try:
                headers={
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9',
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'DNT': '1',
                    'Pragma': 'no-cache',
                    'Sec-Fetch-Dest': 'empty',
                    'Sec-Fetch-Mode': 'cors',
                    'Sec-Fetch-Site': 'same-origin',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
                    'sec-ch-ua': '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"Windows"',
                }
                params = {
                    "announcementId": info["id"]
                }
                response = sess.get(
                    self.detail_url,
                    params=params,
                    cookies=self.cookies,
                    headers=headers)
                if response.status_code == 200:
                    content = response.json()["data"]["announcement"]["announcementHtml"]
                    try:
                        data = self.parse(content)
                        data['procurementMethod'] = GetType().is_type(info['procurementMethod'],info["announcementTitle"])
                        data['announcementTitle'] = info["announcementTitle"]
                        data['releaseTime'] = info["releaseTime"]
                        data['originalWebsiteAddress'] = info["originalWebsiteAddress"]
                        fields_to_check = [
                            "projectName",
                            "releaseSource",
                            'announcementTitle',
                            'releaseTime'
                        ]
                        if data and all(
                                data.get(field) and str(data.get(field)).strip() != '' for field in fields_to_check):
                            # 数据库存储以及Mq上传
                            if data is not None:
                                data.pop('_id', None)
                                data.pop('purchaserAddress', None)
                                self.logo_info['IsValidData'] = 1
                                self.logo_info['WebsiteError'] = 1
                                try:
                                    pass
                                    mongoToMQ(11, data
                                              # ,host ='182.43.38.79', username ='qqbx', password = 'qqbx123123'
                                              )
                                except Exception as e:
                                    print("发送数据异常:", e)
                                try:
                                    pass
                                    self.mongo.insert_one(data)
                                except:
                                    print("数据保存异常！！")
                                self.log.info(f"【￥】最终结果:{data}")
                                ####### 以url字段进行去重
                                self.checker.add_url(originalWebsiteAddress)
                                return
                        else:
                            self.logo_info['IsValidData'] = 0
                            try:
                                self.log.error(
                                    f'【*】异常原因:{originalWebsiteAddress}----:{[f"{field}:{data.get(field)}" for field in fields_to_check]}')
                            except:
                                self.log.error(f"【*】异常原因:{originalWebsiteAddress}")
                            return
                    except Exception as e:
                        self.log.error(f"spider数据处理异常:{e}")
                        self.logo_info['IsValidData'] = 0
                    return
                elif response.status_code in (403, 412):
                    if response.status_code == 412:
                        content, ts_js, jsurl = self.main_rs_info(response.text)
                        self.execjs_data(sess, content, ts_js, jsurl)
                    time.sleep(min(2 ** attempt, 8))
                else:
                    self.logo_info['WebsiteError'] = 0
                    time.sleep(min(2 ** attempt, 8))
            except Exception as e:
                error_msg = str(e)
                print(f"请求失败（尝试 {attempt + 1}/{MAX_RETRIES}）: {error_msg}")
                if attempt == MAX_RETRIES - 1:
                    self.logo_info['WebsiteError'] = 0
                    return False

    def _process_one_item(self, data):
        """处理单条列表数据（供详情线程池调用）"""
        originalWebsiteAddress = "https://ec.chng.com.cn/channel/home/#/detail?id=" + str(data["announcementId"])
        announcementTitle = data["announcementTitle"]
        releaseTime = self.timestamp_to_ymd(data["createtime"], '%Y-%m-%d')
        self.log.info(f"{announcementTitle}---{releaseTime}---{originalWebsiteAddress}")
        procurementMethod = GetType().get_type(announcementTitle)
        if self.checker.is_duplicate(originalWebsiteAddress):
            self.log.info(f"*******存在{originalWebsiteAddress}")
            return
        self.logo_info['HasNewData'] = 1
        info = {
            "id": str(data["announcementId"]),
            "announcementTitle": announcementTitle,
            "announcementType": "招标公告",
            "releaseTime": releaseTime,
            "originalWebsiteAddress": originalWebsiteAddress,
            "procurementMethod": procurementMethod,
        }
        self.detail(info, originalWebsiteAddress)

    # 搜索页面展示
    def spider(self, page, is_send=True):
        sess = self._get_sess()
        for attempt in range(MAX_RETRIES):
            json_data = {
                'start':page*10,
                'limit': 10,
                'type': '103',
                'search': '',
                'ifend': '',
            }
            response = sess.post(self.url,headers=self.headers,cookies=self.cookies,json=json_data,proxies=self.proxy)
            if response.status_code == 200:
                if not is_send:
                    totalpage = math.ceil(int(response.json()["totalCount"]) / 10)
                    return totalpage
                datas = response.json()["root"]
                with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as detail_pool:
                    detail_futures = [
                        detail_pool.submit(self._process_one_item, data)
                        for data in datas
                    ]
                    for future in detail_futures:
                        try:
                            future.result()
                        except Exception as e:
                            self.log.error(f"详情采集异常: {e}")
                return
            if response.status_code == 412:
                content, ts_js, jsurl = self.main_rs_info(response.text)
                self.execjs_data(sess, content, ts_js, jsurl)
            else:
                self.logo_info['WebsiteError'] = 0
                time.sleep(min(2 ** attempt, 8))


    def parse(self,item):
        #附件信息提取
        try:
            fileInfo=self.decryp_html.fileurl(item)
        except:
            fileInfo =None
            pass
        try:
            html_text=self.decryp_html.clearhtml(item)
        except:
            raise RuntimeError("未找到 class='article-main' 的 div")

        htmlContent=self.decryp_html.deal_html(html_text)

        html_text=self.decryp_html.del_script(html_text)

        # 通过传入HTML纯文本，调用大模型
        infodata = self.model.get_result(html_text)
        self.log.info(f"【*】模型返回结果:{infodata}")
        # 获取区域编码以及名称
        area = jio.parse_location(infodata['purchaserAddress'])

        codedata = GetCode(area).load_config()
        if codedata is None:
            provinceCode=""
            code=""
            city_name=""
        else:
            if 'province' in codedata:
                if codedata['province']:
                    provinceCode=codedata['province_code']
                else:
                    provinceCode =""
            else:
                provinceCode=""
            if 'county' in codedata and codedata['county']:
                code=codedata['county_code']
                city_name=codedata['county']
            else:
                code = codedata['city_code']
                city_name = codedata['city']
        # 判断大模型响应的采购方式是否符合规定类型，如果不符合进行替换
        if fileInfo:
            infodata['fileInfo'] = fileInfo
        infodata['contentType'] = 1
        infodata['provinceCode'] = provinceCode
        infodata['regionCode'] = code
        infodata['regionName'] = city_name
        infodata['htmlContent'] = htmlContent
        infodata['webSource'] = self.webSource
        return infodata


    def crawl(self):
        try:
            self._ensure_session()
            totalcount = self.spider(0, False)
            self.log.info(f'共有 --{totalcount} 页数据')
        except Exception as e:
            self.logo_info['WebsiteError'] = 0
            self.logo_info['WasSuccessful'] = 0
            self.log.error(f"爬虫执行失败：{str(e)}")
            totalcount = 0
        if totalcount < 1:
            self.logo_info['HasContent'] = 0
            return
        self.logo_info['HasContent'] = 1
        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as executor:
            futures = {
                executor.submit(self.spider, page, True): page
                for page in range(1, totalcount + 1)
            }
            for future in tqdm(as_completed(futures), total=len(futures), desc="采集进度"):
                page = futures[future]
                try:
                    future.result()
                    self.log.info(f'第【{page}】页采集完成')
                except Exception as e:
                    self.log.error(f'第【{page}】页采集失败: {e}')

        self.log.info(f'监控标识统计：{self.logo_info}')
        # self.storage.update_website_data(
        #     province="测试",
        #     city="测试",
        #     webname=self.webSource,
        #     updates=self.logo_info)


if __name__ == '__main__':
    start_time = time.time()
    HnSpider().crawl()
    print(time.time() - start_time)



