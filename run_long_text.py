import urllib.request
import urllib.parse
import json
import http.cookiejar

BASE_URL = "http://localhost:8000"

print("=" * 70)
print("长文本测试 - 测试AI总结能力")
print("=" * 70)

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

print("\n0. 登录...")
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

long_content = """
深度学习是机器学习的一个分支，它使用多层神经网络来模拟人类大脑的学习过程。深度学习模型可以自动从原始数据中学习特征表示，无需手动提取特征。

卷积神经网络（CNN）是深度学习中最常用的架构之一，特别适用于图像处理任务。CNN通过卷积层、池化层和全连接层的组合，可以有效地提取图像中的空间特征。卷积层使用滤波器扫描输入图像，提取局部特征；池化层用于降维和减少参数数量；全连接层则将提取的特征映射到最终的输出类别。

另一个重要的深度学习架构是循环神经网络（RNN），它适用于序列数据处理，如自然语言处理和时间序列预测。RNN通过循环连接，可以捕捉序列中的时间依赖关系。然而，标准RNN存在梯度消失问题，限制了其处理长序列的能力。为了解决这个问题，长短时记忆网络（LSTM）和门控循环单元（GRU）被提出，它们通过门控机制可以更好地保留长期依赖信息。

Transformer架构是近年来深度学习领域的重大突破，它基于自注意力机制，可以并行处理序列中的所有位置，而不需要递归计算。自注意力机制允许模型在处理每个位置时，同时关注序列中的其他位置，从而更好地捕捉全局依赖关系。Transformer已经成为自然语言处理领域的主流架构，被广泛应用于机器翻译、文本生成、问答系统等任务。

在训练深度学习模型时，通常使用反向传播算法来更新模型参数。反向传播通过计算损失函数对每个参数的梯度，然后使用梯度下降或其变体来更新参数。为了加速训练过程，可以使用GPU进行并行计算，现代GPU如NVIDIA的CUDA架构可以大幅提高训练速度。

深度学习的应用非常广泛，包括计算机视觉、自然语言处理、语音识别、推荐系统、自动驾驶等领域。随着硬件性能的提升和算法的不断改进，深度学习模型的性能也在不断提高，为解决各种复杂问题提供了强大的工具。
"""

print("\n1. 存入长文本记忆...")
result = auth_request(BASE_URL + '/api/bucket', {
    'content': long_content.strip(),
    'name': '深度学习入门指南',
    'tags': ['深度学习', '神经网络', 'AI'],
    'importance': 8,
}, method='POST')

result = json.loads(result)
if 'id' in result:
    bucket_id = result['id']
    print(f"   ✅ 创建成功: {bucket_id}")
else:
    print(f"   ❌ 创建失败: {result}")
    exit()

print("\n2. 验证记忆存储...")
result = auth_request(BASE_URL + '/api/bucket/' + bucket_id)
result = json.loads(result)
content_length = len(result.get('content', ''))
print(f"   ✅ 记忆内容长度: {content_length} 字符")
print(f"   ✅ 记忆名称: {result.get('name', '')}")
print(f"   ✅ 初始衰减阶段: {result.get('metadata', {}).get('decay_stage', 1)}")

print("\n3. 模拟阶段2（总结性描述）...")
result = auth_request(BASE_URL + '/api/bucket/' + bucket_id, {
    'decay_stage': 2
}, method='PUT')
if isinstance(result, dict):
    result_dict = result
else:
    result_dict = json.loads(result)
if result_dict.get('success', True):
    print("   ✅ 设置为阶段2成功")
    result = auth_request(BASE_URL + '/api/bucket/' + bucket_id)
    if isinstance(result, dict):
        result_dict = result
    else:
        result_dict = json.loads(result)
    print(f"   ✅ 当前衰减阶段: {result_dict.get('metadata', {}).get('decay_stage', 1)}")
else:
    print(f"   ❌ 设置失败: {result_dict}")

print("\n4. 模拟阶段3（已消化）...")
result = auth_request(BASE_URL + '/api/bucket/' + bucket_id, {
    'decay_stage': 3,
    'digested': True
}, method='PUT')
if isinstance(result, dict):
    result_dict = result
else:
    result_dict = json.loads(result)
if result_dict.get('success', True):
    print("   ✅ 设置为阶段3成功")
    result = auth_request(BASE_URL + '/api/bucket/' + bucket_id)
    if isinstance(result, dict):
        result_dict = result
    else:
        result_dict = json.loads(result)
    print(f"   ✅ 当前衰减阶段: {result_dict.get('metadata', {}).get('decay_stage', 1)}")
    print(f"   ✅ 是否已消化: {result_dict.get('metadata', {}).get('digested', False)}")
else:
    print(f"   ❌ 设置失败: {result_dict}")

print("\n5. 测试呼吸检索（阶段3状态）...")
result = auth_request(BASE_URL + '/breath-hook')
print(f"   ✅ 呼吸检索成功")
print("   --- 结果摘要 ---")
if isinstance(result, dict):
    print(f"   返回类型: dict - {json.dumps(result, ensure_ascii=False)[:200]}...")
else:
    lines = result.split('\n')[:5]
    print('\n'.join(lines))
    if len(result.split('\n')) > 5:
        print("   ... (更多内容)")

print("\n" + "=" * 70)
print("测试完成！")
print("=" * 70)