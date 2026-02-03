# 安装必要的库和模块
import os
import sys
import ssl
# 解决终端卡死的核心：强制环境直连，不走任何 VPN 或代理
os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
# 需要把http装成.27以下的版本
import re
import json
import io
from openai import OpenAI 

# --- 【防报错补丁】解决 Windows 终端打印中文乱码崩溃问题 ---
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# ================= 调用大模型API配置区域  ===================
API_KEY = "sk-" # 你的 API Key
BASE_URL = "https://api.moonshot.cn/v1"  # API地址 
MODEL_NAME = "kimi-latest" # 模型名称 
# =======================================================

"""======= 程序区：将需要清洗的结果定义为独立的对象 ========="""
class SandwichCleaner:
    def __init__(self, file_path):
        self.file_path = file_path
        self.raw_text = ""
        self.cleaned_text = ""
        self.structured_data = []
        
# 修复：添加自定义 http_client 来处理 SSL 问题
        import httpx
        
        # 创建自定义 SSL 上下文（解决 Windows SSL 问题）
        ssl_context = ssl.create_default_context()
        ssl_context.set_ciphers('DEFAULT@SECLEVEL=1')
        
        # 创建自定义 transport
        transport = httpx.HTTPTransport(verify=ssl_context)
        
        # 创建 httpx client
        http_client = httpx.Client(
            transport=transport,
            timeout=120.0,
            follow_redirects=True
        )

        # 修复：使用标准配置，不要手动传 http_client
        self.client = OpenAI(
            api_key=API_KEY, 
            base_url=BASE_URL,
            timeout=120.0  # 给 60 秒超时，不要 300 秒
        )
        

    def load_data(self):
        """第0层: 读取原始文件"""
        if not os.path.exists(self.file_path):
            print(f"❌ 错误：找不到文件 {self.file_path}")
            return
        with open(self.file_path, 'r', encoding='utf-8') as f:
            self.raw_text = f.read()
        print(f"[1/4] 文件加载成功，共 {len(self.raw_text)} 字符")

    
    def layer1_regex_rough_clean(self):
        """ 【第一层：正则粗洗】目标：给大模型“减负”，去掉显而易见的垃圾干扰。"""
        text = self.raw_text
        # 1. 去除转义符
        text = re.sub(r'\\', '', text)
        # 2. 暴力清除页眉页脚 (包含“论纪说法”、“案件审理室”的整行)
        text = re.sub(r'^.*(论纪说法|案件审理室|作者).*$', '', text, flags=re.MULTILINE)
        # 3. 清除孤立噪点行（如 "过 和六 o"）
        def filter_noise(match):
            line = match.group()
            # 逻辑：如果一行里字数极少(<5)但空格极多(>字数2倍)，视为噪点删除
            chars = len(line.replace(' ', '').strip())
            if chars > 0 and chars < 5 and line.count(' ') > chars * 2:
                return '' 
            return line
        text = re.sub(r'^.*$', filter_noise, text, flags=re.MULTILINE)
        # 4. 修正特定 OCR 错别字（高频错误）
        replacements = {
            r'肥研究': '经研究',
            r'二症吓': '王某某',
            r'爹纸': '金纸',
            r'纪委到': '纪委监委',
            r'弟\s*纪': '党纪',
            r'御级': '升级',
            r'某革': '某某',
        }
        for wrong, right in replacements.items():
            text = re.sub(wrong, right, text)
        # 5. 清除页码行 (如 ".16", "5于")
        text = re.sub(r'^\s*[0-9.:谷涝党。…\s]{1,10}\s*$', '', text, flags=re.MULTILINE)
        # 6. 压缩空行
        text = re.sub(r'\n{3,}', '\n\n', text)
        
        self.cleaned_text = text
        print(f"[2/4] 正则粗洗完成，剩余 {len(self.cleaned_text)} 字符")

    def layer2_llm_semantic_repair(self):
        """第2层: LLM 修复 (支持断点续存：无论是因为超时、断网还是Token耗尽截断)"""
        print("[3/4] 正在连接大模型... (即使输出中断，也会自动保存已生成部分)")
        
        # --- 强力纠错 Prompt (保持不变) ---
        prompt = (
            "你是一名资深的法律文书编辑。待处理文本是识别质量极差的 OCR 扫描件。\n"
            "你还是一个法律案件要素提取专家。请阅读文本，提取所有案例的关键要素。\n"
            "【你的任务】：\n"
            "1. **深度纠错**：把 '王个吓'、'王革姜' 改为 '王某某'；'过和六o' 改为 '过和六'；'爹纸' 改为 '金纸'。\n"
            "2. **提取结构**：请直接使用以下【中文标签】包裹内容，不要输出 JSON，不要输出 Markdown 代码块。\n\n"
            "3. 每个案例格式如下：\n"
            "<案例开始>\n"
            "[案例标题] (内容)\n"
            "[内容提要] (内容)\n"
            "[基本案情] (内容，必须修正人名和错别字)\n"
            "[分歧意见] (内容)\n"
            "[意见分析] (内容)\n"
            "[相关规定] (内容)\n\n"
            "【要求】：\n"
            "- 1.必须包含所有案例。\n"
            "- 2.内容要忠实于原文，但必须顺手修复明显的OCR错别字词。\n"
            "- 3.如果原文不通顺，请直接润色为通顺的法言法语。\n"
            "- 4.如果某个要素缺失，请保留标签但内容留空。\n"
            "- 5.如果文本在结尾处被截断，仍然要尽可能提取已生成的部分。\n" 
            "- 6.直接输出带标签的文本，不要使用 markdown 代码块。\n" 

            f"【待处理文本】：\n{self.cleaned_text}"
        )

        tagged_text = ""
        is_interrupted = False # 标记是否发生意外
        
        try:
            response = self.client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": "你是一个严谨的法律数据处理助手。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.4,
                max_tokens=10000, 
                stream=True,     
                timeout=600.0    
            )
            
            print("   >>> 正在接收数据: ", end="", flush=True)
            for chunk in response:
                # 检查 finish_reason，如果是因为 length 截断，打印提示
                if chunk.choices[0].finish_reason == "length":
                    print("\n⚠️ 警告：输出达到 Token 上限，内容被截断。正在保存已生成部分...")
                    is_interrupted = True
                
                if chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    tagged_text += content
                    print(content, end="", flush=True)
            
            if not is_interrupted:
                print("\n   >>> 生成流程自然结束。")

        except KeyboardInterrupt:
            print("\n\n⚠️ 检测到用户中断 (Ctrl+C)！正在抢救已生成的数据...")
            is_interrupted = True
        except Exception as e:
            # 关键修改：遇到网络报错不 return，而是记录错误，继续去解析已有的文本
            print(f"\n\n❌ API连接异常 (可能是超时或断网): {e}")
            print("⚠️ 正在尝试保存已获取的文本...")
            is_interrupted = True

        # --- 下面是“兜底”逻辑：只要 tagged_text 里有字，就解析 ---
        if not tagged_text:
            print("❌ 未能获取任何有效文本，无法解析。")
            return

        print("\n   >>> 开始解析结构 (包含可能不完整的末尾)...")

        # 1. 切分案例块
        case_splits = re.split(r'<案例开始>', tagged_text)
        case_blocks = [c.strip() for c in case_splits if c.strip()]
        
        self.structured_data = []
        
        for idx, block in enumerate(case_blocks, 1):
            factors = [
                ("标题", "案例标题", False),
                ("内容提要", "内容提要", False),
                ("基本案情", "基本案情", False),
                ("分歧意见", "分歧意见", True),
                ("意见分析", "意见分析", False),
                ("相关规定", "相关规定", True)
            ]
            
            case_data = {}
            has_content = False
            
            for json_key, tag_name, is_list in factors:
                # 正则解释：
                # \s*(.*?)   -> 贪婪匹配内容
                # (?=\[|<|$) -> 直到遇到下一个 '[' 标签，或者 '<' 案例开始，或者字符串结束($)
                # 最后的 $ 符号确保了即使文本在这里断掉，也能匹配到最后所有的字
                pattern = rf"\[{tag_name}\]\s*(.*?)\s*(?=\[|<|$)"
                match = re.search(pattern, block, re.IGNORECASE | re.DOTALL)
                
                content = match.group(1).strip() if match else ""
                
                # 如果这是最后一个案例且被截断了，content 里可能会包含部分未完的句子，这没关系，先存下来
                if content: has_content = True
                
                if is_list and content:
                    if json_key == "分歧意见":
                        # 修复正则语法错误：必须加括号 (?=...)
                        items = re.split(r'(?=第[一二三四五]种意见)', content)
                    else:
                        # 修复逻辑：相关规定按换行切分
                        items = re.split(r'\n', content)
                    case_data[json_key] = [i.strip() for i in items if len(i.strip()) > 2]
                
                elif is_list:
                    case_data[json_key] = []
                else:
                    case_data[json_key] = content

            if not case_data.get("标题"):
                case_data["标题"] = f"案例{idx}"

            # 只要解析出了任何一个字段，就存下来
            if has_content:
                self.structured_data.append(case_data)
        
        print(f"  ✅ 共解析出 {len(self.structured_data)} 个有效案例")
        
    def layer3_save_json(self, output_path):
        """【第三层：保存结果】目标：将修复好的文本拆解为 JSON 格式。"""
        if not self.structured_data:
            print("❌ 没有数据可保存")
            return
            
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(self.structured_data, f, ensure_ascii=False, indent=4)
        print(f"[4/4] 成功提取 {len(self.structured_data)} 个案例至: {output_path}")

"""========= 运行区：脚本运行入口 ==========="""
if __name__ == "__main__":
    # 确保你的文件夹里有 xxx.txt,导入该文件
    cleaner = SandwichCleaner(file_path='shit.txt')
    # 执行读取文件和清洗的四个函数
    cleaner.load_data()
    cleaner.layer1_regex_rough_clean()
    cleaner.layer2_llm_semantic_repair() # 如果没有 API Key，这步会报错但程序不会崩
    cleaner.layer3_save_json('final_clean_data.json')