import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'ytl-blog-secret-2026')

    # Railway 持久化卷路径（挂载 /data 目录后才有值）
    # 如果没有挂载卷，改为用 /tmp 作为临时目录（重启后数据会丢失）
    RAILWAY_VOLUME = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '')

    if RAILWAY_VOLUME:
        DATA_DIR = RAILWAY_VOLUME
    else:
        DATA_DIR = os.path.join(BASE_DIR, 'instance')

    os.makedirs(DATA_DIR, exist_ok=True)

    # 数据库：SQLite，存储在持久化卷中
    SQLALCHEMY_DATABASE_URI = f'sqlite:///{os.path.join(DATA_DIR, "blog.db")}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 上传文件目录（持久化卷或临时目录）
    UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    MAX_CONTENT_LENGTH = 2 * 1024 * 1024 * 1024  # 2GB
    MAX_FORM_MEMORY_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

    ALLOWED_EXTENSIONS = {
        'c', 'h', 'cpp', 'hpp', 'cc', 's', 'asm',
        'bin', 'img', 'hex', 'elf', 'fw',
        'py', 'js', 'ts', 'html', 'css', 'json', 'xml', 'yaml', 'yml',
        'md', 'txt', 'sh', 'bat', 'ps1',
        'zip', 'tar', 'gz', '7z',
        'pdf', 'doc', 'docx', 'xls', 'xlsx',
        'png', 'jpg', 'jpeg', 'gif', 'svg',
        'cmake', 'makefile', 'ini', 'cfg', 'conf', 'log',
    }
