from datetime import datetime, timezone, timedelta
import os
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# 东八区时间
CN_TIMEZONE = timezone(timedelta(hours=8))

def cn_now():
    """返回东八区当前时间"""
    return datetime.now(CN_TIMEZONE)

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    avatar = db.Column(db.String(256), default='')
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=cn_now)
    last_login = db.Column(db.DateTime)

    repos = db.relationship('Repo', backref='owner', lazy='dynamic', cascade='all, delete-orphan')

    def avatar_url(self, req):
        if self.avatar:
            return req.host_url.rstrip('/') + '/static/avatars/' + self.avatar
        return req.host_url.rstrip('/') + '/static/img/default_avatar.png'

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Repo(db.Model):
    """仓库（类似 GitHub repo）"""
    __tablename__ = 'repos'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    is_public = db.Column(db.Boolean, default=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=cn_now, index=True)
    updated_at = db.Column(db.DateTime, default=cn_now, onupdate=cn_now)

    files = db.relationship('RepoFile', backref='repo', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def file_count(self):
        return self.files.count()

    @property
    def total_size(self):
        total = db.session.query(db.func.sum(RepoFile.file_size)).filter_by(repo_id=self.id).scalar() or 0
        return total

    @property
    def total_size_display(self):
        size = self.total_size
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


class RepoFile(db.Model):
    """仓库中的文件（保留目录结构）"""
    __tablename__ = 'repo_files'
    id = db.Column(db.Integer, primary_key=True)
    repo_id = db.Column(db.Integer, db.ForeignKey('repos.id'), nullable=False)
    # 相对路径，如 src/main.c 或 include/config.h
    path = db.Column(db.String(512), nullable=False)
    # 存储在磁盘上的实际文件名
    stored_name = db.Column(db.String(512), nullable=False)
    file_size = db.Column(db.Integer, default=0)
    md5_hash = db.Column(db.String(32))
    upload_user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=cn_now)
    updated_at = db.Column(db.DateTime, default=cn_now, onupdate=cn_now)

    uploader = db.relationship('User', foreign_keys=[upload_user_id])

    @property
    def filename(self):
        """文件名（不含路径）"""
        return self.path.split('/')[-1]

    @property
    def dirname(self):
        """所在目录"""
        parts = self.path.split('/')
        return '/'.join(parts[:-1]) if len(parts) > 1 else ''

    @property
    def file_size_display(self):
        size = self.file_size or 0
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    @property
    def ext(self):
        name = self.filename
        if '.' in name:
            return name.rsplit('.', 1)[1].lower()
        return ''

    @property
    def is_text(self):
        return self.ext in {'c', 'h', 'cpp', 'hpp', 'py', 'js', 'ts', 'html', 'css',
                            'json', 'xml', 'yaml', 'yml', 'md', 'txt', 'sh', 'bat',
                            'cmake', 'makefile', 'ini', 'cfg', 'conf', 'log', 's', 'asm',
                            'csv', 'sql', 'log'}

    @property
    def lang(self):
        mapping = {
            'c': 'c', 'h': 'c', 'cpp': 'cpp', 'hpp': 'cpp',
            'py': 'python', 'js': 'javascript', 'ts': 'typescript',
            'html': 'html', 'css': 'css', 'json': 'json',
            'xml': 'xml', 'yaml': 'yaml', 'yml': 'yaml',
            'md': 'markdown', 'sh': 'bash', 'bat': 'batch',
            's': 'asm', 'asm': 'asm',
        }
        return mapping.get(self.ext, 'plaintext')

    def to_html_table(self, repo_upload_dir):
        """将 xlsx 转为 HTML 表格（保留字体和填充颜色）"""
        import openpyxl
        from openpyxl.styles import Color as OxlColor
        fpath = os.path.join(repo_upload_dir, self.stored_name)
        wb = openpyxl.load_workbook(fpath, data_only=False)
        ws = wb.active
        
        def get_color(color):
            """获取颜色值"""
            if not color:
                return None
            # 空字符串或无意义值
            if hasattr(color, 'rgb') and not color.rgb:
                return None
            # 直接是字符串
            if isinstance(color, str):
                if not color or len(color) < 6:
                    return None
                if len(color) == 8:
                    return f"#{color[2:]}"
                return color
            # RGB 颜色对象
            if hasattr(color, 'rgb') and color.rgb:
                rgb = color.rgb
                if isinstance(rgb, str):
                    if len(rgb) == 8:
                        return f"#{rgb[2:]}"
                    return rgb
            # 主题色
            if hasattr(color, 'theme') and color.theme is not None:
                return None
            return None
        
        html = '<table class="excel-table">'
        for row_idx, row in enumerate(ws.iter_rows()):
            html += '<tr>'
            for cell in row:
                value = cell.value if cell.value is not None else ""
                
                # 字体颜色
                font_color = ""
                if cell.font and cell.font.color:
                    c = get_color(cell.font.color)
                    if c:
                        font_color = f"color:{c};"
                
                # 填充颜色
                bg_color = ""
                if cell.fill and cell.fill.fill_type != 'none':
                    fill = cell.fill
                    # 实心填充用 fgColor
                    if fill.fill_type == 'solid':
                        if fill.fgColor and fill.fgColor.rgb:
                            c = get_color(fill.fgColor)
                            if c:
                                bg_color = f"background-color:{c};"
                                # 黑色背景用白字
                                if c.lower() in ['#000000', '000000']:
                                    font_color = "color:#ffffff;"
                    # 其他类型用 start_color
                    elif fill.fill_type and hasattr(fill, 'start_color') and fill.start_color:
                        c = get_color(fill.start_color)
                        if c:
                            bg_color = f"background-color:{c};"
                
                style = f'style="{font_color}{bg_color}"' if font_color or bg_color else ""
                tag = 'th' if row_idx == 0 else 'td'
                html += f'<{tag} {style}>{value}</{tag}>'
            html += '</tr>'
        html += '</table>'
        wb.close()
        return html


class Category(db.Model):
    """文章分类"""
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    slug = db.Column(db.String(50), unique=True, nullable=False)  # URL友好标识
    description = db.Column(db.String(200))
    order = db.Column(db.Integer, default=0)   # 排序权重
    created_at = db.Column(db.DateTime, default=cn_now)

    posts = db.relationship('Post', backref='category', lazy='dynamic')

    @property
    def post_count(self):
        return self.posts.filter_by(is_public=True).count()


class Post(db.Model):
    """博客文章"""
    __tablename__ = 'posts'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    summary = db.Column(db.String(300))
    tags = db.Column(db.String(200))   # 逗号分隔的标签，如 "Python,Django,全栈开发"
    is_public = db.Column(db.Boolean, default=True)
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'))
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=cn_now, index=True)
    updated_at = db.Column(db.DateTime, default=cn_now, onupdate=cn_now)
    view_count = db.Column(db.Integer, default=0)

    author = db.relationship('User', foreign_keys=[user_id])

    @property
    def tag_list(self):
        """返回标签列表"""
        if not self.tags:
            return []
        return [t.strip() for t in self.tags.split(',') if t.strip()]


class OperationLog(db.Model):
    """操作日志"""
    __tablename__ = 'operation_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    username = db.Column(db.String(80))          # 记录时快照，避免用户删除后无数据
    action = db.Column(db.String(50), nullable=False, index=True)   # login/logout/create_repo/...
    target = db.Column(db.String(200))           # 操作对象描述，如"仓库:xxx"
    detail = db.Column(db.String(500))           # 额外细节
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=cn_now, index=True)


class SyncState(db.Model):
    """同步状态：记录与远端最后一次同步的时间"""
    __tablename__ = 'sync_state'
    id = db.Column(db.Integer, primary_key=True)
    peer_url = db.Column(db.String(256), unique=True, nullable=False)  # 远端地址
    last_sync_at = db.Column(db.DateTime, default=cn_now)  # 上次同步时间
    last_status = db.Column(db.String(20), default='')  # success / error
    last_message = db.Column(db.String(500), default='')  # 状态消息
    updated_at = db.Column(db.DateTime, default=cn_now, onupdate=cn_now)


class SyncDeletion(db.Model):
    """同步删除记录：记录被删除的记录，以便同步到远端"""
    __tablename__ = 'sync_deletions'
    id = db.Column(db.Integer, primary_key=True)
    table_name = db.Column(db.String(80), nullable=False, index=True)  # 表名
    record_id = db.Column(db.Integer, nullable=False)  # 被删除记录的ID
    deleted_at = db.Column(db.DateTime, default=cn_now, index=True)  # 删除时间


class Message(db.Model):
    """留言板"""
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    username = db.Column(db.String(80))           # 发帖时快照
    content = db.Column(db.String(1000), nullable=False)  # 留言内容（限1000字）
    created_at = db.Column(db.DateTime, default=cn_now, index=True)

    author = db.relationship('User', foreign_keys=[user_id])

    user = db.relationship('User', foreign_keys=[user_id])
