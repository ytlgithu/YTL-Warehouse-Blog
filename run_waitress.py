from waitress import serve
from waitress.adjustments import Adjustments
from app import app, _start_sync_thread

if __name__ == '__main__':
    # 设置 2GB 限制
    max_size = 2 * 1024 * 1024 * 1024  # 2GB

    print('启动 Waitress 服务器...')
    print('访问地址: http://127.0.0.1:5000')
    print('局域网地址: http://192.168.10.248:5000')
    print(f'最大请求体大小: {max_size / 1024 / 1024 / 1024:.1f} GB')

    # 验证配置
    adj = Adjustments(max_request_body_size=max_size)
    print(f'实际 max_request_body_size: {adj.max_request_body_size}')

    # 启动后台同步线程
    _start_sync_thread()

    serve(app, host='0.0.0.0', port=5000, threads=4,
          max_request_body_size=max_size)
