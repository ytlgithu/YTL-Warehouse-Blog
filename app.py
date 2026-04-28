import os
import hashlib
import zipfile
import shutil
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, session, send_file, abort, jsonify, Response, make_response)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename

# 东八区时间
CN_TIMEZONE = timezone(timedelta(hours=8))
def cn_now():
    return datetime.now(CN_TIMEZONE)

from config import Config
from models import db, User, Repo, RepoFile, Post, OperationLog, Category, Message, SyncState, SyncDeletion

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

# 修复 Werkzeug 3.x 的 max_form_memory_size 限制
# Werkzeug 的 FormDataParser 默认 max_form_memory_size=None，
# 导致 Werkzeug 内部对单个字段/buffer 有隐式限制，改为 2GB
import werkzeug.formparser
import werkzeug.sansio.multipart
import werkzeug.wsgi
import sys

# Patch Werkzeug's Request class to increase max_form_parts
# Default is 1000, user uploads 3583 files which exceeds this
import werkzeug.wrappers.request
_original_max_form_parts = werkzeug.wrappers.request.Request.max_form_parts
werkzeug.wrappers.request.Request.max_form_parts = 100000  # Increase to 100k
print(f"[DEBUG] Patched max_form_parts: {werkzeug.wrappers.request.Request.max_form_parts}", file=sys.stderr)

# Patch get_input_stream to add debug output
_original_get_input_stream = werkzeug.wsgi.get_input_stream

def _patched_get_input_stream(environ, safe_fallback=True, max_content_length=None):
    from werkzeug.wsgi import get_content_length
    content_length = get_content_length(environ)
    print(f"[DEBUG get_input_stream] content_length={content_length}, max_content_length={max_content_length}", file=sys.stderr)
    return _original_get_input_stream(environ, safe_fallback, max_content_length)

werkzeug.wsgi.get_input_stream = _patched_get_input_stream

# Patch LimitedStream.__init__ to debug
_original_limited_stream_init = None

try:
    import werkzeug.wsgi as wsgi_module
    _original_limited_stream_init = wsgi_module.LimitedStream.__init__
    
    def _patched_limited_stream_init(self, stream, limit, is_max=False):
        print(f"[DEBUG LimitedStream] limit={limit}, is_max={is_max}", file=sys.stderr)
        return _original_limited_stream_init(self, stream, limit, is_max)
    
    wsgi_module.LimitedStream.__init__ = _patched_limited_stream_init
except Exception as e:
    print(f"[DEBUG] Could not patch LimitedStream: {e}", file=sys.stderr)

# Patch FormDataParser and MultipartDecoder
_original_fdp_init = werkzeug.formparser.FormDataParser.__init__
_original_mpd_init = werkzeug.sansio.multipart.MultipartDecoder.__init__

def _patched_fdp_init(self, *args, **kwargs):
    if kwargs.get('max_form_memory_size') is None:
        kwargs['max_form_memory_size'] = 2 * 1024 * 1024 * 1024
    _original_fdp_init(self, *args, **kwargs)

def _patched_mpd_init(self, *args, **kwargs):
    if kwargs.get('max_form_memory_size') is None:
        kwargs['max_form_memory_size'] = 2 * 1024 * 1024 * 1024
    _original_mpd_init(self, *args, **kwargs)

werkzeug.formparser.FormDataParser.__init__ = _patched_fdp_init
werkzeug.sansio.multipart.MultipartDecoder.__init__ = _patched_mpd_init

# Patch MultipartDecoder.receive_data to add debug output
_original_receive_data = werkzeug.sansio.multipart.MultipartDecoder.receive_data

def _patched_receive_data(self, data):
    print(f"[DEBUG receive_data] buffer_len={len(self.buffer)}, data_len={len(data) if data else 0}, max_form_memory_size={self.max_form_memory_size}", file=sys.stderr)
    return _original_receive_data(self, data)

werkzeug.sansio.multipart.MultipartDecoder.receive_data = _patched_receive_data

print("[DEBUG] Patched Werkzeug components for large file upload", file=sys.stderr)

# 错误处理器
@app.errorhandler(413)
def request_entity_too_large(error):
    import sys
    print(f"413 Error: {error}", file=sys.stderr)
    print(f"Content-Length: {request.content_length}", file=sys.stderr)
    print(f"MAX_CONTENT_LENGTH: {app.config.get('MAX_CONTENT_LENGTH')}", file=sys.stderr)
    sys.stderr.flush()
    return jsonify({
        'error': 'Request Entity Too Large',
        'content_length': request.content_length,
        'max_allowed': app.config.get('MAX_CONTENT_LENGTH')
    }), 413

# 请求前钩子
@app.before_request
def log_content_length():
    if request.method == 'POST':
        import sys
        print(f"Before request - Content-Length: {request.content_length}", file=sys.stderr)
        print(f"MAX_CONTENT_LENGTH: {app.config.get('MAX_CONTENT_LENGTH')}", file=sys.stderr)
        sys.stderr.flush()


# ── template filters ──────────────────────────────────────────────────────────

@app.template_filter('avatar_url')
def avatar_url(filename):
    if filename:
        return url_for('static', filename='avatars/' + filename)
    return url_for('static', filename='img/default_avatar.png')


@app.template_filter('markdown')
def markdown_filter(text):
    import re
    # 简单 markdown 转 HTML
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', text)
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.M)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.M)
    text = re.sub(r'^# (.+)$', r'<h1>\1</h1>', text, flags=re.M)
    text = re.sub(r'^- (.+)$', r'<li>\1</li>', text, flags=re.M)
    text = re.sub(r'(<li>.*</li>\n?)+', r'<ul>\g<0></ul>', text)
    text = re.sub(r'```(\w*)\n(.*?)```', r'<pre><code class="language-\1">\2</code></pre>', text, flags=re.S)
    text = re.sub(r'\n\n', r'</p><p>', text)
    text = '<p>' + text + '</p>'
    return text


@app.template_filter('time_since')
def time_since(dt):
    if not dt:
        return ''
    now = cn_now()
    # 如果 dt 是 naive datetime，给它加上时区
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TIMEZONE)
    delta = now - dt
    s = int(delta.total_seconds())
    if s < 60:
        return '刚刚'
    if s < 3600:
        return f'{s // 60}分钟前'
    if s < 86400:
        return f'{s // 3600}小时前'
    if s < 604800:
        return f'{s // 86400}天前'
    return dt.strftime('%Y-%m-%d')


# ── helpers ───────────────────────────────────────────────────────────────────

def compute_md5(filepath):
    h = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('请先登录', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('请先登录', 'warning')
            return redirect(url_for('login'))
        if not session.get('is_admin'):
            flash('需要管理员权限', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def current_user():
    if 'user_id' in session:
        return User.query.get(session['user_id'])
    return None


@app.context_processor
def inject_user():
    return dict(cu=current_user())


def repo_upload_dir(repo_id):
    d = os.path.join(app.config['UPLOAD_FOLDER'], f'repo_{repo_id}')
    os.makedirs(d, exist_ok=True)
    return d


# ── auth ──────────────────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        if not username or not email or not password:
            flash('所有字段不能为空', 'danger')
        elif password != password2:
            flash('两次密码不一致', 'danger')
        elif len(password) < 6:
            flash('密码至少6位', 'danger')
        elif User.query.filter_by(username=username).first():
            flash('用户名已存在', 'danger')
        elif User.query.filter_by(email=email).first():
            flash('邮箱已被注册', 'danger')
        else:
            u = User(username=username, email=email)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash('注册成功，请登录', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        u = User.query.filter_by(username=username).first()
        if u and u.check_password(password):
            session.permanent = bool(request.form.get('remember'))
            session['user_id'] = u.id
            session['username'] = u.username
            session['is_admin'] = u.is_admin
            u.last_login = cn_now()
            db.session.commit()
            log_operation('login', target=u.username)
            flash(f'欢迎回来，{u.username}！', 'success')
            return redirect(url_for('index'))
        flash('用户名或密码错误', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    uname = session.get('username', '?')
    session.clear()
    log_operation('logout', target=uname)
    flash('已退出登录', 'info')
    return redirect(url_for('index'))


# ── 首页 ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    repos = Repo.query.filter_by(is_public=True).order_by(Repo.updated_at.desc()).limit(10).all()
    posts = Post.query.filter_by(is_public=True).order_by(Post.created_at.desc()).limit(5).all()
    stats = {
        'repo_count': Repo.query.count(),
        'user_count': User.query.count(),
        'post_count': Post.query.count(),
        'file_count': RepoFile.query.count(),
    }
    return render_template('index.html', repos=repos, posts=posts, stats=stats)


# ── 关于 ──────────────────────────────────────────────────────────────────────

@app.route('/about')
def about():
    stats = {
        'repo_count': Repo.query.count(),
        'user_count': User.query.count(),
        'post_count': Post.query.count(),
        'file_count': RepoFile.query.count(),
    }
    return render_template('about.html', stats=stats)


# ── 仓库 ──────────────────────────────────────────────────────────────────────

@app.route('/repos')
def repo_list():
    q = request.args.get('q', '').strip()
    query = Repo.query.filter_by(is_public=True)
    if q:
        query = query.filter(Repo.name.contains(q) | Repo.description.contains(q))
    repos = query.order_by(Repo.updated_at.desc()).all()
    return render_template('repo_list.html', repos=repos, q=q)


@app.route('/repos/new', methods=['GET', 'POST'])
@login_required
def repo_new():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        desc = request.form.get('description', '').strip()
        is_public = request.form.get('is_public', '1') == '1'
        if not name:
            flash('仓库名称不能为空', 'danger')
        elif Repo.query.filter_by(user_id=session['user_id'], name=name).first():
            flash('你已有同名仓库', 'danger')
        else:
            repo = Repo(name=name, description=desc, is_public=is_public, user_id=session['user_id'])
            db.session.add(repo)
            db.session.commit()
            log_operation('create_repo', target=f'仓库:{name}')
            flash(f'仓库 {name} 创建成功', 'success')
            return redirect(url_for('repo_view', repo_id=repo.id))
    return render_template('repo_new.html')


@app.route('/repos/<int:repo_id>')
@app.route('/repos/<int:repo_id>/tree/')
@app.route('/repos/<int:repo_id>/tree/<path:subpath>')
def repo_view(repo_id, subpath=''):
    repo = Repo.query.get_or_404(repo_id)
    if not repo.is_public and (not session.get('user_id') or
            (session['user_id'] != repo.user_id and not session.get('is_admin'))):
        abort(403)

    # 获取当前目录下的文件和子目录
    all_files = repo.files.all()
    # 动态检测实际前缀（兼容仓库改名后旧文件路径不一致的情况）
    actual_prefix = None
    for f in all_files:
        if '/' in f.path:
            actual_prefix = f.path.split('/')[0] + '/'
            break
    if actual_prefix is None:
        actual_prefix = repo.name + '/'
    
    # 计算要查找的路径前缀
    # 检查 subpath 是否需要加 actual_prefix 前缀
    if subpath:
        # 检查是否有文件直接以 subpath 开头（不需要 actual_prefix）
        has_direct_path = any(f.path.startswith(subpath + '/') for f in all_files)
        if has_direct_path:
            prefix = subpath + '/'
        else:
            prefix = actual_prefix + subpath + '/'
    else:
        prefix = actual_prefix

    # DEBUG
    import sys
    sys.stderr.write(f'[DEBUG] repo_id={repo_id} subpath={repr(subpath)} actual_prefix={repr(actual_prefix)} prefix={repr(prefix)} total={len(all_files)}\n')
    sys.stderr.flush()

    # 直接子项
    dirs, files = set(), []
    for f in all_files:
        if not f.path.startswith(prefix):
            continue
        rest = f.path[len(prefix):]
        if '/' in rest:
            dirs.add(rest.split('/')[0])
        else:
            files.append(f)

    dirs = sorted(dirs)
    files = sorted(files, key=lambda x: x.filename)

    sys.stderr.write(f'[DEBUG] dirs={dirs[:10]} files_count={len(files)}\n')
    sys.stderr.flush()

    # 面包屑
    breadcrumbs = []
    if subpath:
        parts = subpath.split('/')
        for i, p in enumerate(parts):
            breadcrumbs.append({'name': p, 'path': '/'.join(parts[:i+1])})

    sys.stderr.write(f'[DEBUG] passing to template: dirs={len(dirs)}, files={len(files)}\n')
    sys.stderr.flush()
    # 禁用浏览器缓存，确保面包屑点击时总是获取最新内容
    resp = make_response(render_template('repo_view.html', repo=repo, dirs=dirs, files=files,
                           subpath=subpath, breadcrumbs=breadcrumbs))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/repos/<int:repo_id>/upload', methods=['GET', 'POST'])
@login_required
def repo_upload(repo_id):
    repo = Repo.query.get_or_404(repo_id)
    if repo.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('repo_view', repo_id=repo_id))

    if request.method == 'POST':
        upload_path = request.form.get('upload_path', '').strip().strip('/')
        files = request.files.getlist('files')
        paths = request.form.getlist('paths')  # 前端传来的相对路径

        if not files:
            flash('请选择文件', 'danger')
            return redirect(request.url)

        upload_dir = repo_upload_dir(repo_id)
        # 动态检测实际前缀（兼容仓库改名后旧文件路径不一致的情况）
        existing_files = RepoFile.query.filter_by(repo_id=repo_id).all()
        actual_prefix = None
        for f in existing_files:
            if '/' in f.path:
                actual_prefix = f.path.split('/')[0] + '/'
                break
        if actual_prefix is None:
            actual_prefix = repo.name + '/'
        count = 0
        for file, rel_path in zip(files, paths):
            if not file.filename:
                continue
            # 规范化路径，并加上实际前缀
            rel_path = rel_path.replace('\\', '/').lstrip('/')
            if upload_path:
                full_path = actual_prefix + upload_path + '/' + rel_path
            else:
                full_path = actual_prefix + rel_path

            # 存储文件
            stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{secure_filename(file.filename)}"
            stored_path = os.path.join(upload_dir, stored_name)
            file.save(stored_path)
            size = os.path.getsize(stored_path)
            md5 = compute_md5(stored_path)

            # 检查是否已存在同路径文件（更新）
            existing = RepoFile.query.filter_by(repo_id=repo_id, path=full_path).first()
            if existing:
                # 删除旧文件
                old = os.path.join(upload_dir, existing.stored_name)
                if os.path.exists(old):
                    os.remove(old)
                existing.stored_name = stored_name
                existing.file_size = size
                existing.md5_hash = md5
                existing.updated_at = cn_now()
                existing.upload_user_id = session['user_id']
            else:
                rf = RepoFile(repo_id=repo_id, path=full_path, stored_name=stored_name,
                              file_size=size, md5_hash=md5, upload_user_id=session['user_id'])
                db.session.add(rf)
            count += 1

        repo.updated_at = cn_now()
        db.session.commit()
        log_operation('upload_file', target=f'仓库:{repo.name}', detail=f'上传{count}个文件')
        flash(f'成功上传 {count} 个文件', 'success')
        return redirect(url_for('repo_view', repo_id=repo_id,
                                subpath=upload_path) if upload_path else
                        url_for('repo_view', repo_id=repo_id))

    current_path = request.args.get('path', '')
    return render_template('repo_upload.html', repo=repo, current_path=current_path)


@app.route('/repos/<int:repo_id>/file/<int:file_id>')
def repo_file_view(repo_id, file_id):
    repo = Repo.query.get_or_404(repo_id)
    rf = RepoFile.query.filter_by(id=file_id, repo_id=repo_id).first_or_404()

    content = None
    xlsx_html = None
    if rf.is_text:
        fpath = os.path.join(repo_upload_dir(repo_id), rf.stored_name)
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception:
            content = None
    # xlsx 预览
    if rf.ext == 'xlsx':
        try:
            xlsx_html = rf.to_html_table(repo_upload_dir(repo_id))
        except Exception as e:
            xlsx_html = f"<pre>Excel解析失败: {e}</pre>"

    # 面包屑（动态去掉实际路径前缀）
    breadcrumbs = []
    dirname = rf.dirname
    # 动态检测前缀：取 rf.path 第一个 / 前的部分
    actual_prefix = rf.path[:rf.path.find('/') + 1] if '/' in rf.path else ''
    if actual_prefix and dirname.startswith(actual_prefix):
        dirname = dirname[len(actual_prefix):]
    if dirname:
        parts = dirname.split('/')
        for i, p in enumerate(parts):
            breadcrumbs.append({'name': p, 'path': '/'.join(parts[:i+1])})

    return render_template('repo_file_view.html', repo=repo, rf=rf,
                           content=content, xlsx_html=xlsx_html, breadcrumbs=breadcrumbs)


@app.route('/repos/<int:repo_id>/file/<int:file_id>/edit', methods=['GET', 'POST'])
@login_required
def repo_file_edit(repo_id, file_id):
    repo = Repo.query.get_or_404(repo_id)
    rf = RepoFile.query.filter_by(id=file_id, repo_id=repo_id).first_or_404()
    if repo.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('repo_file_view', repo_id=repo_id, file_id=file_id))

    fpath = os.path.join(repo_upload_dir(repo_id), rf.stored_name)

    if request.method == 'POST':
        new_content = request.form.get('content', '')
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        rf.file_size = os.path.getsize(fpath)
        rf.md5_hash = compute_md5(fpath)
        rf.updated_at = cn_now()
        repo.updated_at = cn_now()
        db.session.commit()
        log_operation('edit_file', target=f'仓库:{repo.name}', detail=f'编辑 {rf.filename}')
        flash('文件已保存', 'success')
        return redirect(url_for('repo_file_view', repo_id=repo_id, file_id=file_id))

    content = ''
    try:
        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception:
        pass
    # 面包屑（动态去掉实际路径前缀）
    breadcrumbs = []
    dirname = rf.dirname
    actual_prefix = rf.path[:rf.path.find('/') + 1] if '/' in rf.path else ''
    if actual_prefix and dirname.startswith(actual_prefix):
        dirname = dirname[len(actual_prefix):]
    if dirname:
        parts = dirname.split('/')
        for i, p in enumerate(parts):
            breadcrumbs.append({'name': p, 'path': '/'.join(parts[:i+1])})

    return render_template('repo_file_edit.html', repo=repo, rf=rf, content=content, breadcrumbs=breadcrumbs)


@app.route('/repos/<int:repo_id>/file/<int:file_id>/download')
def repo_file_download(repo_id, file_id):
    repo = Repo.query.get_or_404(repo_id)
    rf = RepoFile.query.filter_by(id=file_id, repo_id=repo_id).first_or_404()
    fpath = os.path.join(repo_upload_dir(repo_id), rf.stored_name)
    log_operation('download', target=f'仓库:{repo.name}', detail=f'下载 {rf.filename}')
    return send_file(fpath, as_attachment=True, download_name=rf.filename)


@app.route('/repos/<int:repo_id>/file/<int:file_id>/preview')
def repo_file_preview(repo_id, file_id):
    """用于内联预览文件（如 PDF），不触发下载"""
    repo = Repo.query.get_or_404(repo_id)
    rf = RepoFile.query.filter_by(id=file_id, repo_id=repo_id).first_or_404()
    fpath = os.path.join(repo_upload_dir(repo_id), rf.stored_name)
    return send_file(fpath, as_attachment=False, download_name=rf.filename)


@app.route('/repos/<int:repo_id>/download-zip')
@app.route('/repos/<int:repo_id>/download-zip/<path:subpath>')
def repo_download_zip(repo_id, subpath=''):
    repo = Repo.query.get_or_404(repo_id)
    all_files = repo.files.all()
    # 动态检测实际前缀
    actual_prefix = None
    for f in all_files:
        if '/' in f.path:
            actual_prefix = f.path.split('/')[0] + '/'
            break
    if actual_prefix is None:
        actual_prefix = repo.name + '/'
    prefix = actual_prefix + subpath + '/' if subpath else actual_prefix

    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        for rf in all_files:
            if not rf.path.startswith(prefix):
                continue
            fpath = os.path.join(repo_upload_dir(repo_id), rf.stored_name)
            if os.path.exists(fpath):
                # ZIP里去掉实际前缀，只保留 subpath 后的部分
                zip_path = rf.path[len(actual_prefix):] if rf.path.startswith(actual_prefix) else rf.path
                zf.write(fpath, zip_path)
    zip_name = f"{repo.name}{'_' + subpath.replace('/', '_') if subpath else ''}.zip"
    return send_file(tmp.name, as_attachment=True, download_name=zip_name)


@app.route('/repos/<int:repo_id>/file/<int:file_id>/delete', methods=['POST'])
@login_required
def repo_file_delete(repo_id, file_id):
    repo = Repo.query.get_or_404(repo_id)
    rf = RepoFile.query.filter_by(id=file_id, repo_id=repo_id).first_or_404()
    if repo.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('repo_view', repo_id=repo_id))
    fpath = os.path.join(repo_upload_dir(repo_id), rf.stored_name)
    if os.path.exists(fpath):
        os.remove(fpath)
    # 返回上级（去掉数据库中的实际前缀）
    parent = rf.dirname
    if '/' in rf.path:
        db_prefix = rf.path[:rf.path.find('/') + 1]
        if parent.startswith(db_prefix):
            parent = parent[len(db_prefix):]
    log_deletion(rf)
    db.session.delete(rf)
    db.session.commit()
    log_operation('delete_file', target=f'仓库:{repo.name}', detail=f'删除 {rf.filename}')
    flash('文件已删除', 'success')
    if parent:
        return redirect(url_for('repo_view', repo_id=repo_id, subpath=parent))
    return redirect(url_for('repo_view', repo_id=repo_id))


@app.route('/repos/<int:repo_id>/delete-folder', methods=['POST'])
@login_required
def repo_folder_delete(repo_id):
    """删除文件夹及其下所有文件"""
    repo = Repo.query.get_or_404(repo_id)
    if repo.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('repo_view', repo_id=repo_id))
    
    folder_path = request.form.get('path', '').strip('/')
    if not folder_path:
        flash('不能删除根目录', 'danger')
        return redirect(url_for('repo_view', repo_id=repo_id))
    
    # 删除该前缀下的所有文件（需加实际前缀）
    upload_dir = repo_upload_dir(repo_id)
    # 动态检测实际前缀
    existing = RepoFile.query.filter_by(repo_id=repo_id).all()
    actual_prefix = None
    for f in existing:
        if '/' in f.path:
            actual_prefix = f.path.split('/')[0] + '/'
            break
    if actual_prefix is None:
        actual_prefix = repo.name + '/'
    db_prefix = actual_prefix + folder_path + '/'
    files_to_delete = RepoFile.query.filter(
        RepoFile.repo_id == repo_id,
        RepoFile.path.startswith(db_prefix)
    ).all()
    
    for rf in files_to_delete:
        fpath = os.path.join(upload_dir, rf.stored_name)
        if os.path.exists(fpath):
            os.remove(fpath)
        log_deletion(rf)
        db.session.delete(rf)
    
    db.session.commit()
    log_operation('delete_folder', target=f'仓库:{repo.name}', detail=f'删除文件夹 {folder_path}（含{len(files_to_delete)}个文件）')
    flash(f'已删除文件夹 {folder_path} 及 {len(files_to_delete)} 个文件', 'success')
    
    # 返回上一级目录（去掉前缀）
    parent = '/'.join(folder_path.split('/')[:-1])
    return redirect(url_for('repo_view', repo_id=repo_id, subpath=parent))


@app.route('/repos/<int:repo_id>/toggle-visibility', methods=['POST'])
@login_required
def repo_toggle_visibility(repo_id):
    """切换仓库公开/私有状态"""
    repo = Repo.query.get_or_404(repo_id)
    if repo.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('repo_view', repo_id=repo_id))
    
    repo.is_public = not repo.is_public
    db.session.commit()
    status = '公开' if repo.is_public else '私有'
    log_operation('toggle_repo_visibility', target=f'仓库:{repo.name}', detail=f'设为{status}')
    flash(f'仓库已设为{status}', 'success')
    return redirect(url_for('repo_view', repo_id=repo_id))


@app.route('/repos/<int:repo_id>/delete', methods=['POST'])
@login_required
def repo_delete(repo_id):
    repo = Repo.query.get_or_404(repo_id)
    if repo.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('repo_list'))
    log_operation('delete_repo', target=f'仓库:{repo.name}')
    upload_dir = repo_upload_dir(repo_id)
    if os.path.exists(upload_dir):
        shutil.rmtree(upload_dir)
    log_deletion(repo)
    db.session.delete(repo)
    db.session.commit()
    flash(f'仓库 {repo.name} 已删除', 'success')
    return redirect(url_for('repo_list'))


# ── 博客 ──────────────────────────────────────────────────────────────────────

@app.route('/posts')
def post_list():
    q = request.args.get('q', '').strip()
    cat_slug = request.args.get('cat', '').strip()
    query = Post.query.filter_by(is_public=True)
    if q:
        query = query.filter(Post.title.contains(q) | Post.content.contains(q))
    if cat_slug:
        cat = Category.query.filter_by(slug=cat_slug).first_or_404()
        query = query.filter_by(category_id=cat.id)
    else:
        cat = None
    posts = query.order_by(Post.created_at.desc()).all()
    categories = Category.query.order_by(Category.order.asc()).all()
    return render_template('post_list.html', posts=posts, q=q, cat=cat, categories=categories)


@app.route('/posts/new', methods=['GET', 'POST'])
@login_required
def post_new():
    categories = Category.query.order_by(Category.order.asc()).all()
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        summary = request.form.get('summary', '').strip() or content[:150]
        category_id = request.form.get('category_id', type=int)
        tags = request.form.get('tags', '').strip()
        if not title or not content:
            flash('标题和内容不能为空', 'danger')
        else:
            post = Post(title=title, content=content, summary=summary,
                        category_id=category_id or None,
                        tags=tags or None,
                        user_id=session['user_id'])
            db.session.add(post)
            db.session.commit()
            log_operation('create_post', target=f'文章:{title}')
            flash('文章发布成功', 'success')
            return redirect(url_for('post_detail', post_id=post.id))
    return render_template('post_edit.html', post=None, categories=categories)


@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    post = Post.query.get_or_404(post_id)
    # 私有文章只有作者和管理员能看
    if not post.is_public:
        if 'user_id' not in session or (post.user_id != session['user_id'] and not session.get('is_admin')):
            flash('该文章为私有，无权查看', 'danger')
            return redirect(url_for('post_list'))
    post.view_count += 1
    db.session.commit()
    return render_template('post_detail.html', post=post)


@app.route('/posts/<int:post_id>/edit', methods=['GET', 'POST'])
@login_required
def post_edit(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('post_detail', post_id=post_id))
    if request.method == 'POST':
        post.title = request.form.get('title', '').strip()
        post.content = request.form.get('content', '').strip()
        post.summary = request.form.get('summary', '').strip() or post.content[:150]
        post.category_id = request.form.get('category_id', type=int) or None
        post.tags = request.form.get('tags', '').strip() or None
        post.updated_at = cn_now()
        db.session.commit()
        log_operation('update_post', target=f'文章:{post.title}', detail='编辑文章')
        flash('文章已更新', 'success')
        return redirect(url_for('post_detail', post_id=post_id))
    categories = Category.query.order_by(Category.order.asc()).all()
    return render_template('post_edit.html', post=post, categories=categories)


@app.route('/posts/<int:post_id>/delete', methods=['POST'])
@login_required
def post_delete(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('post_list'))
    log_deletion(post)
    db.session.delete(post)
    db.session.commit()
    log_operation('delete_post', target=f'文章:{post.title}', detail='删除文章')
    flash('文章已删除', 'success')
    return redirect(url_for('post_list'))


# ── 文章图片上传 ───────────────────────────────────────────────────────────────

@app.route('/posts/upload-image', methods=['POST'])
@login_required
def post_upload_image():
    """上传文章图片，返回 Markdown 格式的链接"""
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '没有上传文件'}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'error': '请选择文件'}), 400
    
    # 检查文件类型
    allowed = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'}
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    if ext not in allowed:
        return jsonify({'success': False, 'error': '不支持的图片格式'}), 400
    
    # 生成安全文件名
    original_name = secure_filename(file.filename)
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    hash_name = hashlib.md5((timestamp + original_name).encode()).hexdigest()[:8]
    filename = f"{hash_name}_{original_name}"
    
    # 保存到 uploads/post_images/
    upload_dir = os.path.join(app.root_path, 'uploads', 'post_images')
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    
    # 返回 Markdown 格式的图片链接
    image_url = f'/uploads/post_images/{filename}'
    return jsonify({
        'success': True,
        'url': image_url,
        'markdown': f'![{original_name}]({image_url})'
    })


# ── 上传文件访问 ───────────────────────────────────────────────────────────────

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """访问上传的文件"""
    return send_file(os.path.join(app.root_path, 'uploads', filename))


@app.route('/posts/<int:post_id>/toggle-visibility', methods=['POST'])
@login_required
def post_toggle_visibility(post_id):
    """切换文章公开/私有状态"""
    post = Post.query.get_or_404(post_id)
    if post.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('post_detail', post_id=post_id))
    
    post.is_public = not post.is_public
    db.session.commit()
    status = '公开' if post.is_public else '私有'
    log_operation('toggle_post_visibility', target=f'文章:{post.title}', detail=f'设为{status}')
    flash(f'文章已设为{status}', 'success')
    return redirect(url_for('post_detail', post_id=post_id))


# ── 用户 ──────────────────────────────────────────────────────────────────────

@app.route('/users')
@admin_required
def user_list():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('user_list.html', users=users)


@app.route('/admin/user/<int:user_id>/toggle-admin', methods=['POST'])
@admin_required
def toggle_user_admin(user_id):
    target = User.query.get_or_404(user_id)
    # 超级管理员（username=admin）不可被任何人操作
    if target.username == 'admin':
        flash('超级管理员权限不可修改', 'danger')
        return redirect(url_for('user_list'))
    target.is_admin = not target.is_admin
    db.session.commit()
    log_operation('toggle_admin', target=f'用户:{target.username}',
                  detail=f"设置{target.username}为{'管理员' if target.is_admin else '普通用户'}")
    flash(f'已更新 {target.username} 的权限', 'success')
    return redirect(url_for('user_list'))


@app.route('/profile')
@login_required
def profile():
    u = current_user()
    repos = u.repos.order_by(Repo.updated_at.desc()).all()
    posts = Post.query.filter_by(user_id=u.id).order_by(Post.created_at.desc()).all()
    return render_template('profile.html', user=u, repos=repos, posts=posts)

ALLOWED_AVATAR = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
AVATAR_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'avatars')

@app.route('/avatar/upload', methods=['POST'])
@login_required
def avatar_upload():
    os.makedirs(AVATAR_FOLDER, exist_ok=True)
    if 'avatar' not in request.files:
        flash('没有选择文件', 'danger')
        return redirect(url_for('profile'))
    f = request.files['avatar']
    if f.filename == '':
        flash('没有选择文件', 'danger')
        return redirect(url_for('profile'))
    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_AVATAR:
        flash('只支持 png/jpg/gif/webp 格式', 'danger')
        return redirect(url_for('profile'))
    import uuid
    stored = f'{uuid.uuid4().hex}.{ext}'
    u = current_user()
    if u.avatar:
        old = os.path.join(AVATAR_FOLDER, u.avatar)
        if os.path.exists(old):
            os.remove(old)
    f.save(os.path.join(AVATAR_FOLDER, stored))
    u.avatar = stored
    db.session.commit()
    flash('头像已更新', 'success')
    return redirect(url_for('profile'))


# ── 操作日志 ────────────────────────────────────────────────────────────────

MAX_LOG_COUNT = 200

def log_operation(action, target='', detail='', user_id=None):
    """记录操作日志，自动清理超过200条的旧记录"""
    try:
        from flask import request
        ip = request.remote_addr or ''
    except Exception:
        ip = ''
    u = current_user() if user_id is None else None
    uid = user_id if user_id is not None else (u.id if u else None)
    uname = u.username if u else (User.query.get(user_id).username if user_id else '系统')

    log = OperationLog(user_id=uid, username=uname, action=action,
                       target=target, detail=detail, ip_address=ip)
    db.session.add(log)
    db.session.commit()   # 先提交日志本身

    # 自动清理：只保留最新200条
    total = OperationLog.query.count()
    if total > MAX_LOG_COUNT:
        old_ids = [r.id for r in
                   OperationLog.query.order_by(OperationLog.created_at.asc())
                   .limit(total - MAX_LOG_COUNT).all()]
        if old_ids:
            OperationLog.query.filter(OperationLog.id.in_(old_ids)).delete(
                synchronize_session='fetch')
            db.session.commit()


def log_deletion(obj):
    """记录删除操作到 SyncDeletion 表，用于双向同步"""
    try:
        table_name = obj.__tablename__
        record_id = obj.id
        sd = SyncDeletion(table_name=table_name, record_id=record_id)
        db.session.add(sd)
        # 不 commit，由调用方统一 commit
    except Exception:
        pass  # 同步删除记录非关键，静默失败


# ── 操作日志页面 ───────────────────────────────────────────────────────────────

@app.route('/admin/logs')
@admin_required
def operation_log():
    page = request.args.get('page', 1, type=int)
    per_page = 20
    pagination = OperationLog.query.order_by(
        OperationLog.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False)
    return render_template('operation_log.html', pagination=pagination)


# ── init db ───────────────────────────────────────────────────────────────────

def migrate_db():
    """数据库迁移：为已有数据库添加新表/字段"""
    with app.app_context():
        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        
        # 1. 检查 categories 表是否存在，不存在则创建
        if 'categories' not in inspector.get_table_names():
            db.create_all()
            print('[MIGRATE] Created categories table')
        
        # 2. 检查 posts 表是否有 category_id 字段
        columns = [c['name'] for c in inspector.get_columns('posts')]
        if 'category_id' not in columns:
            db.session.execute(text('ALTER TABLE posts ADD COLUMN category_id INTEGER REFERENCES categories(id)'))
            db.session.commit()
            print('[MIGRATE] Added category_id to posts table')

        # 3. 检查 posts 表是否有 tags 字段
        if 'tags' not in columns:
            db.session.execute(text('ALTER TABLE posts ADD COLUMN tags VARCHAR(200)'))
            db.session.commit()
            print('[MIGRATE] Added tags to posts table')

        # 4. PostgreSQL 列名修复（Railway 旧表列名与模型不匹配）
        db_url = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        if db_url.startswith('postgresql'):
            # posts.excerpt → summary
            if 'excerpt' in columns and 'summary' not in columns:
                try:
                    db.session.execute(text('ALTER TABLE posts RENAME COLUMN excerpt TO summary'))
                    db.session.commit()
                    print('[MIGRATE] Renamed posts.excerpt → summary')
                except Exception as e:
                    db.session.rollback()
                    print(f'[MIGRATE] posts.excerpt rename failed (may not exist): {e}')
            # posts.author_id → user_id
            if 'author_id' in columns and 'user_id' not in columns:
                try:
                    db.session.execute(text('ALTER TABLE posts RENAME COLUMN author_id TO user_id'))
                    db.session.commit()
                    print('[MIGRATE] Renamed posts.author_id → user_id')
                except Exception as e:
                    db.session.rollback()
                    print(f'[MIGRATE] posts.author_id rename failed: {e}')
            # categories.sort_order → "order"
            cat_columns = [c['name'] for c in inspector.get_columns('categories')]
            if 'sort_order' in cat_columns and 'order' not in cat_columns:
                try:
                    db.session.execute(text('ALTER TABLE categories RENAME COLUMN sort_order TO "order"'))
                    db.session.commit()
                    print('[MIGRATE] Renamed categories.sort_order → order')
                except Exception as e:
                    db.session.rollback()
                    print(f'[MIGRATE] categories.sort_order rename failed: {e}')

        print('[MIGRATE] Migration complete')


# ============================================================
# 留言板
# ============================================================

@app.route('/messages')
def message_board():
    page = request.args.get('page', 1, type=int)
    per_page = 20
    pagination = Message.query.order_by(Message.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False)
    return render_template('message_board.html', pagination=pagination)


@app.route('/messages', methods=['POST'])
@login_required
def post_message():
    content_text = request.form.get('content', '').strip()
    if not content_text:
        flash('留言内容不能为空', 'warning')
        return redirect(url_for('message_board'))
    if len(content_text) > 1000:
        flash('留言内容不能超过1000字', 'warning')
        return redirect(url_for('message_board'))

    u = current_user()
    msg = Message(user_id=u.id, username=u.username, content=content_text)
    db.session.add(msg)
    db.session.commit()
    log_operation('create_message', target='留言板', detail='发布留言')
    flash('留言已发布', 'success')
    return redirect(url_for('message_board'))


@app.route('/messages/<int:msg_id>/delete', methods=['POST'])
@login_required
def delete_message(msg_id):
    msg = Message.query.get_or_404(msg_id)
    u = current_user()
    if msg.user_id != u.id and not u.is_admin:
        flash('无权删除他人留言', 'danger')
        return redirect(url_for('message_board'))
    log_deletion(msg)
    db.session.delete(msg)
    db.session.commit()
    log_operation('delete_message', target='留言板', detail='删除留言')
    flash('留言已删除', 'success')
    return redirect(url_for('message_board'))



def init_db():
    with app.app_context():
        db.create_all()
        migrate_db()  # 执行迁移
        
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@ytl.local', is_admin=True)
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print('[OK] Admin created: admin / admin123')
        # 创建默认文章分类
        default_cats = [
            {'name': 'Python', 'slug': 'python', 'description': 'Python 编程相关', 'order': 1},
            {'name': 'C/C++', 'slug': 'c-cpp', 'description': 'C 和 C++ 语言', 'order': 2},
            {'name': '嵌入式', 'slug': 'embedded', 'description': '单片机 / RTOS / 驱动', 'order': 3},
            {'name': '前端', 'slug': 'frontend', 'description': 'HTML / CSS / JS / Vue', 'order': 4},
            {'name': '运维 & 工具', 'slug': 'devops', 'description': '服务器 / Docker / Git', 'order': 5},
            {'name': '杂谈', 'slug': 'misc', 'description': '其他技术内容', 'order': 99},
        ]
        for c in default_cats:
            if not Category.query.filter_by(slug=c['slug']).first():
                db.session.add(Category(**c))
        db.session.commit()

        # 自动重置 PostgreSQL 序列（防止 UniqueViolation）
        db_uri = os.environ.get('DATABASE_URL', '')
        if db_uri and ('postgresql' in db_uri or 'postgres' in db_uri):
            tables_seqs = [
                ('users', 'users_id_seq'),
                ('categories', 'categories_id_seq'),
                ('posts', 'posts_id_seq'),
                ('repos', 'repos_id_seq'),
                ('repo_files', 'repo_files_id_seq'),
                ('operation_logs', 'operation_logs_id_seq'),
                ('messages', 'messages_id_seq'),
            ]
            for tbl, seq in tables_seqs:
                try:
                    max_id = db.session.execute(db.text(f'SELECT COALESCE(MAX(id), 0) FROM {tbl}')).scalar()
                    if max_id > 0:
                        db.session.execute(db.text(f"SELECT setval('{seq}', {max_id})"))
                        print(f'[SEQ] {seq} reset to {max_id}')
                except Exception as e:
                    print(f'[SEQ] {seq} skip: {e}')
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        print('[INFO] Database ready')


# Railway (Gunicorn) 启动时也执行
try:
    init_db()
except Exception as e:
    print(f'[ERROR] init_db failed: {e}', file=sys.stderr)

# ========== 分类管理（仅管理员）==========

@app.route('/categories')
@admin_required
def category_list():
    categories = Category.query.order_by(Category.order.asc()).all()
    return render_template('category_list.html', categories=categories)


@app.route('/admin/category/new', methods=['POST'])
@admin_required
def category_new():
    name = request.form.get('name', '').strip()
    slug = request.form.get('slug', '').strip()
    description = request.form.get('description', '').strip()
    # 自动取最大序号+1
    max_order = db.session.query(db.func.max(Category.order)).scalar() or 0
    order = max_order + 1
    if not name or not slug:
        flash('名称和标识不能为空', 'danger')
        return redirect(url_for('category_list'))
    if Category.query.filter_by(slug=slug).first():
        flash('标识已存在', 'danger')
        return redirect(url_for('category_list'))
    cat = Category(name=name, slug=slug, description=description, order=order)
    db.session.add(cat)
    db.session.commit()
    log_operation('category_new', target=f'分类:{name}', detail=f'创建分类「{name}」')
    flash(f'分类「{name}」创建成功', 'success')
    return redirect(url_for('category_list'))


@app.route('/admin/category/<int:cat_id>/edit', methods=['POST'])
@admin_required
def category_edit(cat_id):
    cat = Category.query.get_or_404(cat_id)
    name = request.form.get('name', '').strip()
    slug = request.form.get('slug', '').strip()
    description = request.form.get('description', '').strip()
    order = request.form.get('order', 0, type=int)
    if not name or not slug:
        flash('名称和标识不能为空', 'danger')
        return redirect(url_for('category_list'))
    if Category.query.filter(Category.slug == slug, Category.id != cat_id).first():
        flash('标识已存在', 'danger')
        return redirect(url_for('category_list'))
    cat.name = name
    cat.slug = slug
    cat.description = description
    cat.order = order
    db.session.commit()
    log_operation('category_edit', target=f'分类:{name}', detail=f'编辑分类「{name}」')
    flash(f'分类「{name}」已更新', 'success')
    return redirect(url_for('category_list'))


@app.route('/admin/category/<int:cat_id>/delete', methods=['POST'])
@admin_required
def category_delete(cat_id):
    cat = Category.query.get_or_404(cat_id)
    name = cat.name
    # 有文章的分类不能删
    if cat.posts.count() > 0:
        flash(f'分类「{name}」下有文章，无法删除', 'danger')
        return redirect(url_for('category_list'))
    log_deletion(cat)
    db.session.delete(cat)
    db.session.commit()
    log_operation('category_delete', target=f'分类:{name}', detail=f'删除分类「{name}」')
    flash(f'分类「{name}」已删除', 'success')
    return redirect(url_for('category_list'))


# ========== 数据导出/导入（JSON文件方案）==========

@app.route('/admin/export', methods=['GET'])
@admin_required
def admin_export():
    """导出本地数据为 JSON 文件下载"""
    from flask import Response
    import json as _json
    from datetime import datetime as _dt

    def _ser(obj):
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        raise TypeError(f'Type {type(obj)} not serializable')

    data = {}

    # Users
    data['users'] = [{
        'id': u.id, 'username': u.username,
        'email': u.email or '', 'password_hash': u.password_hash,
        'avatar': u.avatar or '',
        'is_admin': bool(u.is_admin) if u.is_admin is not None else False,
        'created_at': u.created_at, 'last_login': u.last_login
    } for u in User.query.all()]

    # Categories
    data['categories'] = [{
        'id': c.id, 'name': c.name, 'slug': c.slug or '',
        'description': c.description or '',
        'order': c.order if hasattr(c, 'order') else 0,
        'created_at': c.created_at
    } for c in Category.query.all()]

    # Posts
    data['posts'] = [{
        'id': p.id, 'title': p.title, 'content': p.content,
        'summary': p.summary if hasattr(p, 'summary') else '',
        'tags': p.tags or '',
        'is_public': bool(p.is_public) if p.is_public is not None else True,
        'category_id': p.category_id, 'user_id': p.user_id,
        'view_count': p.view_count or 0,
        'created_at': p.created_at, 'updated_at': p.updated_at
    } for p in Post.query.all()]

    # Repos
    data['repos'] = [{
        'id': r.id, 'name': r.name, 'description': r.description or '',
        'is_public': bool(r.is_public) if r.is_public is not None else True,
        'user_id': r.user_id,
        'created_at': r.created_at, 'updated_at': r.updated_at
    } for r in Repo.query.all()]

    # Messages
    try:
        data['messages'] = [{
            'id': m.id, 'user_id': m.user_id, 'username': m.username or '',
            'content': m.content or '', 'created_at': m.created_at
        } for m in Message.query.all()]
    except Exception:
        data['messages'] = []

    # OperationLogs
    try:
        data['operation_logs'] = [{
            'id': l.id, 'user_id': l.user_id, 'username': l.username or '',
            'action': l.action or '', 'target': l.target or '',
            'detail': l.detail or '', 'ip_address': l.ip_address or '',
            'created_at': l.created_at
        } for l in OperationLog.query.all()]
    except Exception:
        data['operation_logs'] = []

    json_str = _json.dumps(data, ensure_ascii=False, indent=2, default=_ser)
    filename = f'blog_export_{_dt.now().strftime("%Y%m%d_%H%M%S")}.json'

    flash(f'导出成功: {len(data["users"])}用户 {len(data["categories"])}分类 {len(data["posts"])}文章 {len(data["repos"])}仓库', 'success')
    return Response(json_str, mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


@app.route('/admin/import', methods=['GET', 'POST'])
@admin_required
def admin_import():
    """从上传的 JSON 文件导入数据到当前数据库"""
    import json as _json

    if request.method == 'GET':
        return render_template('import_data.html')

    file = request.files.get('json_file')
    if not file or not file.filename.endswith('.json'):
        flash('请上传 .json 文件', 'danger')
        return redirect(url_for('admin_import'))

    try:
        raw = file.read().decode('utf-8')
        data = _json.loads(raw)
    except Exception as e:
        flash(f'JSON解析失败: {e}', 'danger')
        return redirect(url_for('admin_import'))

    results = []
    errors = []

    from datetime import datetime as _dt
    _datetime_fields = {'created_at', 'updated_at', 'last_login'}

    def _parse_val(v, key):
        if isinstance(v, str) and key in _datetime_fields:
            try:
                return _dt.fromisoformat(v)
            except (ValueError, TypeError):
                return None
        return v

    def _do_import(model, items, field_map):
        count = 0
        for item in items:
            try:
                obj = model.query.get(item.get('id'))
                if obj:
                    for jk, ma in field_map.items():
                        if jk in item:
                            setattr(obj, ma, _parse_val(item[jk], jk))
                else:
                    kwargs = {ma: _parse_val(item[jk], jk) for jk, ma in field_map.items() if jk in item}
                    obj = model(**kwargs)
                    db.session.add(obj)
                db.session.flush()
                count += 1
            except Exception as ex:
                db.session.rollback()
                errors.append(f'{model.__name__} id={item.get("id")}: {ex}')
        return count

    # Users
    try:
        n = _do_import(User, data.get('users', []), {
            'id': 'id', 'username': 'username', 'email': 'email',
            'password_hash': 'password_hash', 'avatar': 'avatar',
            'is_admin': 'is_admin', 'created_at': 'created_at',
            'last_login': 'last_login'
        })
        results.append(f'用户: {n}条')
    except Exception as e:
        errors.append(f'用户导入失败: {e}')

    # Categories
    try:
        n = _do_import(Category, data.get('categories', []), {
            'id': 'id', 'name': 'name', 'slug': 'slug',
            'description': 'description', 'order': 'order',
            'created_at': 'created_at'
        })
        results.append(f'分类: {n}条')
    except Exception as e:
        errors.append(f'分类导入失败: {e}')

    # Posts
    try:
        n = _do_import(Post, data.get('posts', []), {
            'id': 'id', 'title': 'title', 'content': 'content',
            'summary': 'summary', 'tags': 'tags', 'is_public': 'is_public',
            'category_id': 'category_id', 'user_id': 'user_id',
            'view_count': 'view_count', 'created_at': 'created_at',
            'updated_at': 'updated_at'
        })
        results.append(f'文章: {n}条')
    except Exception as e:
        errors.append(f'文章导入失败: {e}')

    # Repos
    try:
        n = _do_import(Repo, data.get('repos', []), {
            'id': 'id', 'name': 'name', 'description': 'description',
            'is_public': 'is_public', 'user_id': 'user_id',
            'created_at': 'created_at', 'updated_at': 'updated_at'
        })
        results.append(f'仓库: {n}条')
    except Exception as e:
        errors.append(f'仓库导入失败: {e}')

    # Messages
    try:
        n = _do_import(Message, data.get('messages', []), {
            'id': 'id', 'user_id': 'user_id', 'username': 'username',
            'content': 'content', 'created_at': 'created_at'
        })
        results.append(f'留言: {n}条')
    except Exception:
        pass

    # OperationLogs
    try:
        n = _do_import(OperationLog, data.get('operation_logs', []), {
            'id': 'id', 'user_id': 'user_id', 'username': 'username',
            'action': 'action', 'target': 'target', 'detail': 'detail',
            'ip_address': 'ip_address', 'created_at': 'created_at'
        })
        results.append(f'操作日志: {n}条')
    except Exception:
        pass

    try:
        db.session.commit()
        flash('数据导入成功!', 'success')
        # 重置 PostgreSQL 序列（导入数据后序列不同步会导致 duplicate key 错误）
        try:
            db_uri = os.environ.get('DATABASE_URL', '')
            if db_uri and ('postgresql' in db_uri or 'postgres' in db_uri):
                tables_seqs = [
                    ('users', 'users_id_seq'),
                    ('categories', 'categories_id_seq'),
                    ('posts', 'posts_id_seq'),
                    ('repos', 'repos_id_seq'),
                    ('repo_files', 'repo_files_id_seq'),
                    ('operation_logs', 'operation_logs_id_seq'),
                    ('messages', 'messages_id_seq'),
                ]
                for tbl, seq in tables_seqs:
                    try:
                        max_id = db.session.execute(db.text(f'SELECT COALESCE(MAX(id), 0) FROM {tbl}')).scalar()
                        db.session.execute(db.text(f"SELECT setval('{seq}', {max_id})"))
                    except Exception:
                        pass
                db.session.commit()
                flash('  🔧 PostgreSQL 序列已重置', 'success')
        except Exception as seq_err:
            flash(f'  ⚠️ 序列重置失败(不影响数据): {seq_err}', 'warning')
    except Exception as e:
        db.session.rollback()
        flash(f'导入失败(已回滚): {e}', 'danger')

    for r in results:
        flash(f'  ✅ {r}', 'success')
    for e in errors:
        flash(f'  ❌ {e}', 'danger')

    return redirect(url_for('admin_import'))




# ========== 数据同步 API（双向自动同步）==========

# 同步认证 token（两端必须一致）
SYNC_TOKEN = os.environ.get('SYNC_TOKEN', 'ytl-sync-2026-secret')

# 同步的数据表配置：表名 → (模型类, 时间字段名)
SYNC_TABLES = [
    ('users', User, 'created_at'),
    ('categories', Category, 'created_at'),
    ('posts', Post, 'updated_at'),
    ('repos', Repo, 'updated_at'),
    ('repo_files', RepoFile, 'updated_at'),
    ('messages', Message, 'created_at'),
    ('operation_logs', OperationLog, 'created_at'),
]


def _sync_auth():
    """验证同步请求的 token"""
    token = request.headers.get('X-Sync-Token', '') or request.args.get('token', '')
    if token != SYNC_TOKEN:
        abort(403, description='Invalid sync token')


def _ser_model(obj, fields):
    """序列化一个模型实例为字典"""
    result = {}
    for f in fields:
        v = getattr(obj, f, None)
        if v is not None and hasattr(v, 'isoformat'):
            v = v.isoformat()
        result[f] = v
    return result


# 每个表的字段映射
SYNC_FIELDS = {
    'users': ['id', 'username', 'email', 'password_hash', 'avatar', 'is_admin', 'created_at', 'last_login'],
    'categories': ['id', 'name', 'slug', 'description', 'order', 'created_at'],
    'posts': ['id', 'title', 'content', 'summary', 'tags', 'is_public', 'category_id', 'user_id', 'view_count', 'created_at', 'updated_at'],
    'repos': ['id', 'name', 'description', 'is_public', 'user_id', 'created_at', 'updated_at'],
    'repo_files': ['id', 'repo_id', 'path', 'stored_name', 'file_size', 'md5_hash', 'upload_user_id', 'created_at', 'updated_at'],
    'messages': ['id', 'user_id', 'username', 'content', 'created_at'],
    'operation_logs': ['id', 'user_id', 'username', 'action', 'target', 'detail', 'ip_address', 'created_at'],
}


@app.route('/api/sync', methods=['GET'])
def api_sync_pull():
    """返回自给定时间戳以来的所有变更（供远端拉取）
    
    参数:
      since - ISO8601 时间戳，返回此时间之后的变更
      token - 认证 token
    """
    _sync_auth()
    
    since_str = request.args.get('since', '')
    since = None
    if since_str:
        try:
            since = datetime.fromisoformat(since_str)
        except (ValueError, TypeError):
            abort(400, description='Invalid since timestamp')
    
    result = {'changes': {}, 'deletions': []}
    
    # 查询各表变更
    for table_name, model_cls, time_field in SYNC_TABLES:
        query = model_cls.query
        if since:
            col = getattr(model_cls, time_field)
            query = query.filter(col > since)
        records = query.all()
        fields = SYNC_FIELDS.get(table_name, [])
        if records:
            result['changes'][table_name] = [_ser_model(r, fields) for r in records]
    
    # 查询删除记录
    del_query = SyncDeletion.query
    if since:
        del_query = del_query.filter(SyncDeletion.deleted_at > since)
    deletions = del_query.all()
    result['deletions'] = [{
        'table_name': d.table_name,
        'record_id': d.record_id,
        'deleted_at': d.deleted_at.isoformat() if d.deleted_at else None
    } for d in deletions]
    
    return jsonify(result)


@app.route('/api/sync/apply', methods=['POST'])
def api_sync_apply():
    """应用远端推送的变更"""
    _sync_auth()
    
    import json as _json
    data = request.get_json(force=True)
    if not data:
        return jsonify({'status': 'error', 'message': 'No data'}), 400
    
    _datetime_fields = {'created_at', 'updated_at', 'last_login'}
    
    def _parse_val(v, key):
        if isinstance(v, str) and key in _datetime_fields:
            try:
                return datetime.fromisoformat(v)
            except (ValueError, TypeError):
                return None
        return v
    
    results = {'applied': 0, 'errors': []}
    
    # 构建表名 → 模型类 的映射
    model_map = {t: m for t, m, _ in SYNC_TABLES}
    
    # 应用变更
    changes = data.get('changes', {})
    for table_name, records in changes.items():
        model_cls = model_map.get(table_name)
        if not model_cls:
            continue
        fields = SYNC_FIELDS.get(table_name, [])
        for item in records:
            try:
                obj = model_cls.query.get(item.get('id'))
                if obj:
                    # 更新已有记录 — 仅当远端数据更新时才覆盖（基于 updated_at 比较）
                    remote_updated = _parse_val(item.get('updated_at'), 'updated_at')
                    local_updated = getattr(obj, 'updated_at', None)
                    if remote_updated and local_updated and remote_updated <= local_updated:
                        # 远端数据不比本地新，跳过（避免本地较新的数据被覆盖）
                        continue
                    # 对于没有 updated_at 的表（如 users/categories/messages/operation_logs），
                    # 用 created_at 做比较；如果都没有则仍然覆盖
                    if remote_updated is None and local_updated is None:
                        # 无时间戳可比较的表，仍然执行覆盖（保持原有行为）
                        pass
                    elif remote_updated is None and local_updated is not None:
                        # 本地有 updated_at 但远端没有，说明远端数据可能是旧的，跳过
                        continue
                    for f in fields:
                        if f in item and f != 'id':
                            setattr(obj, f, _parse_val(item[f], f))
                else:
                    # 创建新记录
                    kwargs = {f: _parse_val(item[f], f) for f in fields if f in item}
                    obj = model_cls(**kwargs)
                    db.session.add(obj)
                db.session.flush()
                results['applied'] += 1
            except Exception as ex:
                db.session.rollback()
                results['errors'].append(f'{table_name} id={item.get("id")}: {ex}')
    
    # 应用删除
    deletions = data.get('deletions', [])
    for d in deletions:
        table_name = d.get('table_name')
        record_id = d.get('record_id')
        model_cls = model_map.get(table_name)
        if not model_cls or not record_id:
            continue
        try:
            obj = model_cls.query.get(record_id)
            if obj:
                db.session.delete(obj)
                db.session.flush()
                results['applied'] += 1
            # 删除本地的 SyncDeletion 记录（已应用）
            SyncDeletion.query.filter_by(
                table_name=table_name, record_id=record_id).delete()
        except Exception as ex:
            db.session.rollback()
            results['errors'].append(f'delete {table_name} id={record_id}: {ex}')
    
    try:
        db.session.commit()
        # PostgreSQL 序列重置
        db_uri = os.environ.get('DATABASE_URL', '')
        if db_uri and ('postgresql' in db_uri or 'postgres' in db_uri):
            for tbl, seq in [('users', 'users_id_seq'), ('categories', 'categories_id_seq'),
                             ('posts', 'posts_id_seq'), ('repos', 'repos_id_seq'),
                             ('repo_files', 'repo_files_id_seq'),
                             ('operation_logs', 'operation_logs_id_seq'),
                             ('messages', 'messages_id_seq')]:
                try:
                    max_id = db.session.execute(
                        db.text(f'SELECT COALESCE(MAX(id), 0) FROM {tbl}')).scalar()
                    if max_id > 0:
                        db.session.execute(
                            db.text(f"SELECT setval('{seq}', {max_id})"))
                except Exception:
                    pass
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
    return jsonify({'status': 'ok', 'results': results})


@app.route('/admin/sync-status')
@admin_required
def sync_status_page():
    """同步状态页面"""
    states = SyncState.query.all()
    return render_template('sync_status.html',
                           states=states,
                           config_token=SYNC_TOKEN[:6] + '***' if len(SYNC_TOKEN) > 6 else '***',
                           peer_url=os.environ.get('SYNC_PEER_URL', ''),
                           sync_interval=os.environ.get('SYNC_INTERVAL', '60'))


# ========== 后台同步线程（仅本地运行）==========

def _run_sync_loop():
    """后台同步线程：每60秒向远端推送本地变更并拉取远端变更"""
    import time
    import requests as _requests
    
    peer_url = os.environ.get('SYNC_PEER_URL', '')  # 远端地址，如 https://xxx.up.railway.app
    sync_token = SYNC_TOKEN
    interval = int(os.environ.get('SYNC_INTERVAL', '10'))  # 秒
    
    if not peer_url:
        print('[SYNC] No SYNC_PEER_URL set, sync thread disabled')
        return
    
    print(f'[SYNC] Background sync thread started, peer={peer_url}, interval={interval}s')
    
    while True:
        time.sleep(interval)
        try:
            with app.app_context():
                # 获取上次同步时间
                state = SyncState.query.filter_by(peer_url=peer_url).first()
                if state and state.last_sync_at:
                    since = state.last_sync_at.isoformat()
                else:
                    since = ''
                
                # 记录本次同步开始时间（拉取前的时间点）
                sync_start = cn_now()
                
                # 1. 拉取远端变更
                try:
                    pull_url = f'{peer_url.rstrip("/")}/api/sync?since={since}&token={sync_token}'
                    resp = _requests.get(pull_url, timeout=30)
                    if resp.status_code == 200:
                        remote_data = resp.json()
                        # 应用远端变更到本地
                        if remote_data.get('changes') or remote_data.get('deletions'):
                            apply_url = f'{"http://127.0.0.1:5000"}/api/sync/apply'
                            _requests.post(apply_url, json=remote_data,
                                         headers={'X-Sync-Token': sync_token}, timeout=30)
                            print(f'[SYNC] Pulled changes from remote')
                    else:
                        print(f'[SYNC] Pull failed: HTTP {resp.status_code}')
                except Exception as e:
                    print(f'[SYNC] Pull error: {e}')
                
                # 2. 先更新同步时间点，这样推送时 since 之后只包含本地真正产生的变更
                #    （避免把刚从远端拉下来的数据又推回去）
                try:
                    if not state:
                        state = SyncState(peer_url=peer_url)
                        db.session.add(state)
                    state.last_sync_at = sync_start
                    state.last_status = 'syncing'
                    state.last_message = 'Push in progress'
                    db.session.commit()
                except Exception as e:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                    print(f'[SYNC] State update error: {e}')
                
                # 3. 推送本地变更到远端（使用新的 since = sync_start）
                try:
                    # 用 sync_start 之后的变更，排除已同步的数据
                    push_pull_url = f'{"http://127.0.0.1:5000"}/api/sync?since={sync_start.isoformat()}&token={sync_token}'
                    local_resp = _requests.get(push_pull_url, timeout=30)
                    if local_resp.status_code == 200:
                        local_data = local_resp.json()
                        if local_data.get('changes') or local_data.get('deletions'):
                            apply_url = f'{peer_url.rstrip("/")}/api/sync/apply'
                            push_resp = _requests.post(apply_url, json=local_data,
                                                      headers={'X-Sync-Token': sync_token}, timeout=30)
                            if push_resp.status_code == 200:
                                print(f'[SYNC] Pushed changes to remote')
                            else:
                                print(f'[SYNC] Push failed: HTTP {push_resp.status_code}')
                except Exception as e:
                    print(f'[SYNC] Push error: {e}')
                
                # 4. 更新同步最终状态
                try:
                    state.last_sync_at = cn_now()
                    state.last_status = 'success'
                    state.last_message = 'Sync completed'
                    db.session.commit()
                except Exception as e:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                    print(f'[SYNC] State update error: {e}')
                    
        except Exception as e:
            print(f'[SYNC] Loop error: {e}')


# 启动后台同步线程（仅本地 Waitress 运行时）
def _start_sync_thread():
    import threading
    t = threading.Thread(target=_run_sync_loop, daemon=True)
    t.start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
