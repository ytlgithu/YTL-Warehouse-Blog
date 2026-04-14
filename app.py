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
from models import db, User, Repo, RepoFile, Post, OperationLog

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
    if rf.is_text:
        fpath = os.path.join(repo_upload_dir(repo_id), rf.stored_name)
        try:
            with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception:
            content = None

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
                           content=content, breadcrumbs=breadcrumbs)


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
    db.session.delete(repo)
    db.session.commit()
    flash(f'仓库 {repo.name} 已删除', 'success')
    return redirect(url_for('repo_list'))


# ── 博客 ──────────────────────────────────────────────────────────────────────

@app.route('/posts')
def post_list():
    q = request.args.get('q', '').strip()
    query = Post.query.filter_by(is_public=True)
    if q:
        query = query.filter(Post.title.contains(q) | Post.content.contains(q))
    posts = query.order_by(Post.created_at.desc()).all()
    return render_template('post_list.html', posts=posts, q=q)


@app.route('/posts/new', methods=['GET', 'POST'])
@login_required
def post_new():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        summary = request.form.get('summary', '').strip() or content[:150]
        if not title or not content:
            flash('标题和内容不能为空', 'danger')
        else:
            post = Post(title=title, content=content, summary=summary, user_id=session['user_id'])
            db.session.add(post)
            db.session.commit()
            log_operation('create_post', target=f'文章:{title}')
            flash('文章发布成功', 'success')
            return redirect(url_for('post_detail', post_id=post.id))
    return render_template('post_edit.html', post=None)


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
        post.updated_at = cn_now()
        db.session.commit()
        log_operation('update_post', target=f'文章:{post.title}', detail='编辑文章')
        flash('文章已更新', 'success')
        return redirect(url_for('post_detail', post_id=post_id))
    return render_template('post_edit.html', post=post)


@app.route('/posts/<int:post_id>/delete', methods=['POST'])
@login_required
def post_delete(post_id):
    post = Post.query.get_or_404(post_id)
    if post.user_id != session['user_id'] and not session.get('is_admin'):
        flash('无权限', 'danger')
        return redirect(url_for('post_list'))
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

def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', email='admin@ytl.local', is_admin=True)
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
            print('[OK] Admin created: admin / admin123')
        else:
            print('[INFO] Database ready')


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
