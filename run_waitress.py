from waitress import serve
from waitress.adjustments import Adjustments
from app import app, _start_sync_thread
import os

if __name__ == '__main__':
    # 配置同步环境变量（本地启动时生效）
    if not os.environ.get('SYNC_PEER_URL'):
        os.environ['SYNC_PEER_URL'] = 'https://ytl-warehouse-blog-system.up.railway.app'
    if not os.environ.get('SYNC_TOKEN'):
        os.environ['SYNC_TOKEN'] = 'ytl-sync-2026-secret'
    if not os.environ.get('SYNC_INTERVAL'):
        os.environ['SYNC_INTERVAL'] = '10'
    # 重新加载 app config（确保环境变量生效）
    app.config['SYNC_PEER_URL'] = os.environ.get('SYNC_PEER_URL', '')
    app.config['SYNC_TOKEN'] = os.environ.get('SYNC_TOKEN', '')
    app.config['SYNC_INTERVAL'] = int(os.environ.get('SYNC_INTERVAL', '10'))
    # 设置 2GB 限制
    max_size = 2 * 1024 * 1024 * 1024  # 2GB

    print('启动 Waitress 服务器...')
    print('访问地址: http://127.0.0.1:5000')
    print('局域网地址: http://192.168.10.248:5000')
    print(f'最大请求体大小: {max_size / 1024 / 1024 / 1024:.1f} GB')

    # 验证配置
    adj = Adjustments(max_request_body_size=max_size)
    print(f'实际 max_request_body_size: {adj.max_request_body_size}')

    # 启动后台同步线程（环境变量已在上文设置）
    _start_sync_thread()

    serve(app, host='0.0.0.0', port=5000, threads=4,
          max_request_body_size=max_size)
