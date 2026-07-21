import urllib.request
import urllib.parse
import json
import http.cookiejar
import time
import os

BASE_URL = "http://localhost:8000"

print("=" * 80)
print("完整记忆浮现测试 - 所有模块效果演示")
print("=" * 80)

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

print("\n【步骤1】登录系统...")
req = urllib.request.Request(BASE_URL + '/auth/login', method='POST')
req.add_header('Content-Type', 'application/json')
req.data = json.dumps({'password': os.environ.get('TEST_PASSWORD', 'password')}).encode('utf-8')
try:
    resp = opener.open(req)
    print("   ✅ 登录成功")
except urllib.error.HTTPError as e:
    print(f"   ❌ 登录失败: {e.code}")
    exit()

def auth_request(url, data=None, method='GET'):
    req = urllib.request.Request(url, method=method)
    if data:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(data).encode('utf-8')
    try:
        resp = opener.open(req)
        return resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except (json.JSONDecodeError, Exception):
            return {'error': e.code, 'message': e.read().decode('utf-8')}

print("\n【步骤2】创建各模块测试数据...")

print("\n   2.1 创建身份档案...")
result = auth_request(BASE_URL + '/api/bucket', {
    'content': """姓名：张三\n年龄：30岁\n职业：软件工程师\n性格：开朗、细心、责任感强\n爱好：编程、阅读、运动\n技能：Python、JavaScript、数据库设计\n目标：成为技术专家，带领团队完成优秀项目""",
    'name': '张三',
    'tags': ['身份', '个人档案'],
    'type': 'identity',
    'importance': 10,
    'domain': ['个人']
}, method='POST')
result_dict = json.loads(result) if isinstance(result, str) else result
if 'id' in result_dict:
    print(f"   ✅ 身份档案创建成功: {result_dict['id']}")

print("\n   2.2 创建模式(Pattern)...")
result = auth_request(BASE_URL + '/api/bucket', {
    'content': """编程效率提升模式：\n1. 每天早晨花15分钟规划今日任务\n2. 使用番茄工作法（25分钟工作+5分钟休息）\n3. 遇到难题先记录下来，不要死磕超过30分钟\n4. 每天下班前花10分钟总结完成情况\n5. 每周五进行一次代码审查和技术分享\n效果：工作效率提升约40%，代码质量明显提高""",
    'name': '编程效率提升模式',
    'tags': ['模式', '效率', '工作方法'],
    'type': 'pattern',
    'importance': 9,
    'domain': ['工作']
}, method='POST')
result_dict = json.loads(result) if isinstance(result, str) else result
if 'id' in result_dict:
    print(f"   ✅ 模式创建成功: {result_dict['id']}")

print("\n   2.3 创建年轮(经验)...")
result = auth_request(BASE_URL + '/api/bucket', {
    'content': """经验标题：代码审查的重要性\n经验类型：工作经验\n应用次数：5\n上次应用：2026-07-15\n经验内容：通过多次项目实践发现，严格的代码审查可以：\n1. 提前发现潜在bug，减少线上问题\n2. 促进团队知识共享，提升整体技术水平\n3. 保持代码风格一致，提高可维护性\n4. 培养新人的代码质量意识\n建议：每次代码提交前至少进行一次代码审查""",
    'name': '代码审查的重要性',
    'tags': ['经验', '工作', '最佳实践'],
    'type': 'experience',
    'exp_type': '工作经验',
    'apply_count': 5,
    'importance': 8,
    'domain': ['工作']
}, method='POST')
result_dict = json.loads(result) if isinstance(result, str) else result
if 'id' in result_dict:
    print(f"   ✅ 年轮创建成功: {result_dict['id']}")

print("\n   2.4 创建烛台(备忘录)...")
result = auth_request(BASE_URL + '/api/bucket', {
    'content': """标题：下周重要事项\n内容：\n1. 周一上午9点：项目周会\n2. 周二下午2点：客户演示\n3. 周三：数据库迁移\n4. 周四：代码审查\n5. 周五：中期评审\n备注：提前准备演示材料和评审文档""",
    'name': '下周重要事项',
    'tags': ['备忘', '提醒', '工作'],
    'type': 'candlestick',
    'importance': 7,
    'domain': ['工作']
}, method='POST')
result_dict = json.loads(result) if isinstance(result, str) else result
if 'id' in result_dict:
    print(f"   ✅ 烛台创建成功: {result_dict['id']}")

print("\n   2.5 创建高情绪动态记忆...")
high_emotion_memories = [
    {
        'name': '成功完成重要项目',
        'content': """今天成功完成了一个重要的客户项目！整个团队加班加点，克服了很多技术难题，最终按时交付了高质量的产品。客户非常满意，还说要给我们介绍新客户。这是我工作以来最有成就感的一天！""",
        'tags': ['工作', '成就', '团队'],
        'importance': 10,
        'domain': ['工作'],
        'valence': 0.9,
        'arousal': 0.85
    },
    {
        'name': '家庭聚会',
        'content': """周末和家人一起聚会，父母身体都很好，孩子们也很开心。一起做饭、聊天、玩游戏，感觉非常温馨。这种家庭时光是最珍贵的，希望以后能经常这样。""",
        'tags': ['家庭', '幸福', '生活'],
        'importance': 9,
        'domain': ['生活'],
        'valence': 0.85,
        'arousal': 0.7
    },
    {
        'name': '学习新技能',
        'content': """今天学会了使用Docker部署应用！之前一直觉得容器化很复杂，但是通过实践发现其实并没有想象中那么难。成功部署了第一个应用后，感觉非常有成就感。接下来要继续学习Kubernetes。""",
        'tags': ['学习', '技术', '成长'],
        'importance': 8,
        'domain': ['学习'],
        'valence': 0.8,
        'arousal': 0.75
    }
]

for mem in high_emotion_memories:
    result = auth_request(BASE_URL + '/api/bucket', mem, method='POST')
    result_dict = json.loads(result) if isinstance(result, str) else result
    if 'id' in result_dict:
        print(f"   ✅ 高情绪记忆: {mem['name']} (唤醒度:{mem['arousal']})")

print("\n   2.6 创建普通动态记忆...")
normal_memories = [
    {
        'name': '日常会议记录',
        'content': """今日团队会议要点：讨论了下周工作计划，分配了各个任务。需要注意的是，前端团队需要加快进度，确保按时交付。""",
        'tags': ['工作', '会议'],
        'importance': 5,
        'domain': ['工作'],
        'valence': 0.5,
        'arousal': 0.3
    },
    {
        'name': '购物清单',
        'content': """需要购买的物品：牛奶、面包、鸡蛋、蔬菜、水果。记得带环保袋。""",
        'tags': ['生活', '购物'],
        'importance': 3,
        'domain': ['生活'],
        'valence': 0.5,
        'arousal': 0.2
    }
]

for mem in normal_memories:
    result = auth_request(BASE_URL + '/api/bucket', mem, method='POST')
    result_dict = json.loads(result) if isinstance(result, str) else result
    if 'id' in result_dict:
        print(f"   ✅ 普通记忆: {mem['name']}")

print("\n【步骤3】模拟AI记忆浮现（调用breath-hook）...")
time.sleep(2)
result = auth_request(BASE_URL + '/breath-hook')

print("\n" + "=" * 80)
print("记忆浮现结果")
print("=" * 80)
print(result)

print("\n" + "=" * 80)
print("测试完成！")
print("=" * 80)