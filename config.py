import os


BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'ytl-blog-secret-2026')

    # Railway/Vercel PostgreSQL via DATABASE_URL. Local development uses SQLite.
    db_url = os.environ.get('DATABASE_URL', '')
    if db_url:
        uri = db_url.replace('postgres://', 'postgresql://', 1)
        uri = uri.replace('postgresql+pg8000://', 'postgresql://', 1)
        if 'sslmode=' not in uri:
            separator = '&' if '?' in uri else '?'
            uri = f'{uri}{separator}sslmode=require'
        SQLALCHEMY_DATABASE_URI = uri
    else:
        DATA_DIR = os.path.join(BASE_DIR, 'instance')
        os.makedirs(DATA_DIR, exist_ok=True)
        SQLALCHEMY_DATABASE_URI = f'sqlite:///{os.path.join(DATA_DIR, "blog.db")}'

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Railway persistent volume / Vercel temporary filesystem.
    _vol = os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', '')
    _vercel = os.environ.get('VERCEL', '')
    if _vol:
        UPLOAD_FOLDER = os.path.join(_vol, 'uploads')
    elif _vercel:
        UPLOAD_FOLDER = '/tmp/uploads'
    else:
        UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
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
