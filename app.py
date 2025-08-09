from datetime import date, datetime, time as dtime
import os
from flask import Flask, render_template, request, redirect, url_for, session as flask_session, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy import CheckConstraint, Enum, and_, func
from sqlalchemy.orm import validates
from dotenv import load_dotenv
from datetime import timedelta



load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///pilates.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

ALLOWED_STATUSES = ('active', 'canceled', 'moved', 'attended', 'no_show')

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# ——— Models ———
class Session(db.Model):
    __tablename__ = 'sessions'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    time = db.Column(db.Time, nullable=False)
    capacity = db.Column(db.Integer, nullable=False)
    spots_left = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.String(255))
    completed = db.Column(db.Boolean, default=False, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint('capacity >= 0'),
        CheckConstraint('spots_left >= 0'),
        CheckConstraint('spots_left <= capacity'),
    )

    @property
    def is_past(self):
        dt = datetime.combine(self.date, self.time)
        return dt < datetime.now()

    def __repr__(self):
        return f"<Session {self.date} {self.time} cap={self.capacity} left={self.spots_left}>"

class Reservation(db.Model):
    __tablename__ = 'reservations'
    id = db.Column(db.Integer, primary_key=True)
    user_name = db.Column(db.String(120), nullable=False, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False, index=True)
    status = db.Column(Enum(*ALLOWED_STATUSES, name='reservation_status'), default='active', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    session = db.relationship('Session', backref=db.backref('reservations', cascade='all, delete-orphan'))

    @validates('user_name')
    def normalize_name(self, key, value):
        return value.strip()
    
class Member(db.Model):
    __tablename__ = 'members'
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False, unique=True, index=True)
    credits = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @staticmethod
    def canonical(name: str) -> str:
        # trim + tek boşluk + title-case (İ/ı Türkçe başlıklama için özel durumları atlıyoruz)
        return " ".join(name.strip().split())
    
# --- Otomatik Tamamlandı + kredi düşürme ---
@app.before_request
def close_past_sessions_and_apply_attendance():
    now = datetime.now()

    # Tamamlanmamış ve zamanı geçmiş seanslar
    to_close = (
        Session.query
        .filter(
            Session.completed.is_(False),
            (Session.date < now.date()) |
            and_(Session.date == now.date(), Session.time < now.time())
        )
        .all()
    )

    if not to_close:
        return  # yapılacak iş yok

    for s in to_close:
        s.completed = True
        for r in s.reservations:
            if r.status == 'active':
                r.status = 'attended'
                # Üye kredisini 1 düş
                m = Member.query.filter(
                    func.lower(Member.full_name) == r.user_name.lower()
                ).first()
                if m and (m.credits or 0) > 0:
                    m.credits -= 1

    db.session.commit()



# ——— Helpers & Decorators ———
from functools import wraps

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_name' not in flask_session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not flask_session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return wrapper

# ——— Routes: Auth ———
@app.route('/')
def home():
    if flask_session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))
    if 'user_name' in flask_session:
        return redirect(url_for('user_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form.get('user_name', '').strip()
        if not name:
            flash('Lütfen ad–soyad girin.', 'error')
            return redirect(url_for('login'))

        canon = Member.canonical(name)
        member = Member.query.filter(func.lower(Member.full_name) == canon.lower()).first()
        if not member:
            flash('Üyeler listesinde bulunmuyorsunuz. Lütfen hocayla iletişime geçin.', 'error')
            return redirect(url_for('login'))

        flask_session['user_name'] = member.full_name  # üyedeki standardize isim
        flash(f'Hoş geldin, {member.full_name}!', 'success')
        return redirect(url_for('user_dashboard'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    flask_session.clear()
    flash('Çıkış yapıldı.', 'info')
    return redirect(url_for('login'))

# ——— Routes: User ———
@app.route('/dashboard')
@login_required
def user_dashboard():
    name = flask_session['user_name']

    # aktif (gelecek) rezervasyonlar
    my_active = (
        Reservation.query
        .filter_by(user_name=name, status='active')
        .join(Session)
        .order_by(Session.date.asc(), Session.time.asc())
        .all()
    )

    # yaklaşan seanslar
    upcoming = (
        Session.query
        .filter(Session.date >= date.today())
        .order_by(Session.date.asc(), Session.time.asc())
        .all()
    )

    # üye + kalan kredi
    member = Member.query.filter(func.lower(Member.full_name) == name.lower()).first()
    credits_left = member.credits if member else 0

    # bu ay attended sayısı
    first_day = date.today().replace(day=1)
    if first_day.month == 12:
        next_month = first_day.replace(year=first_day.year+1, month=1, day=1)
    else:
        next_month = first_day.replace(month=first_day.month+1, day=1)

    monthly_attended = (
        db.session.query(Reservation)
        .join(Session, Reservation.session_id == Session.id)
        .filter(
            Reservation.user_name == name,
            Reservation.status == 'attended',
            Session.date >= first_day,
            Session.date < next_month
        )
        .count()
    )

    return render_template(
        'user_dashboard.html',
        name=name,
        my_active=my_active,
        upcoming=upcoming,
        credits_left=credits_left,
        monthly_attended=monthly_attended
    )


@app.route('/sessions')
@login_required
def list_sessions():
    upcoming = (
        Session.query
        .filter(Session.date >= date.today())
        .order_by(Session.date.asc(), Session.time.asc())
        .all()
    )
    return render_template('sessions.html', sessions=upcoming)

@app.route('/reserve/<int:session_id>', methods=['POST'])
@login_required
def reserve(session_id):
    s = Session.query.get_or_404(session_id)
    if s.completed or s.is_past:
        flash('Geçmiş/bitmiş seansa kayıt olunamaz.', 'error')
        return redirect(url_for('user_dashboard'))
    if s.spots_left <= 0:
        flash('Bu seans dolu.', 'error')
        return redirect(url_for('user_dashboard'))

    # Üye kredi kontrolü
    # Üye kredi kontrolü — import YOK
    member = Member.query.filter(
        func.lower(Member.full_name) == flask_session['user_name'].lower()
    ).first()
    if not member or member.credits <= 0:
        flash('Seans hakkınız kalmamış. Lütfen hocanızla iletişime geçin.', 'error')
        return redirect(url_for('user_dashboard'))


    existing = Reservation.query.filter_by(
        user_name=flask_session['user_name'], session_id=session_id, status='active'
    ).first()
    if existing:
        flash('Zaten bu seanstasınız.', 'info')
        return redirect(url_for('user_dashboard'))

    r = Reservation(user_name=flask_session['user_name'], session_id=session_id, status='active')
    db.session.add(r)
    s.spots_left -= 1
    db.session.commit()
    flash('Kayıt oluşturuldu ✅', 'success')
    return redirect(url_for('user_dashboard'))


@app.route('/cancel/<int:reservation_id>', methods=['POST'])
@login_required
def cancel(reservation_id):
    r = Reservation.query.get_or_404(reservation_id)
    if r.user_name != flask_session['user_name']:
        flash('Bu işlem için yetkiniz yok.', 'error')
        return redirect(url_for('user_dashboard'))
    if r.status != 'active':
        flash('Bu rezervasyon zaten aktif değil.', 'info')
        return redirect(url_for('user_dashboard'))
    # 24 saat kala kullanıcı iptali yasak
    session_dt = datetime.combine(r.session.date, r.session.time)
    if session_dt - datetime.now() < timedelta(hours=24):
        flash('Seans başlamaya 24 saatten az kaldığı için iptal kullanıcılara kapalı. Lütfen hocayla iletişime geçin.', 'error')
        return redirect(url_for('user_dashboard'))

    if r.session.is_past:
        flash('Geçmiş seans iptal edilemez.', 'error')
        return redirect(url_for('user_dashboard'))
    r.status = 'canceled'
    r.session.spots_left += 1
    db.session.commit()
    flash('Rezervasyon iptal edildi.', 'success')
    return redirect(url_for('user_dashboard'))

@app.route('/move/<int:reservation_id>', methods=['GET', 'POST'])
@login_required
def move(reservation_id):
    r = Reservation.query.get_or_404(reservation_id)
    if r.user_name != flask_session['user_name'] or r.status != 'active':
        flash('İşlem yapılamadı.', 'error')
        return redirect(url_for('user_dashboard'))
    if request.method == 'POST':
        target_id = int(request.form.get('target_id'))
        target = Session.query.get_or_404(target_id)
        if target.is_past:
            flash('Geçmiş seansa taşınamaz.', 'error')
            return redirect(url_for('move', reservation_id=reservation_id))
        if target.spots_left <= 0:
            flash('Hedef seans dolu.', 'error')
            return redirect(url_for('move', reservation_id=reservation_id))
        # taşımayı yap
        r.status = 'moved'
        r.session.spots_left += 1
        new_r = Reservation(user_name=r.user_name, session_id=target.id, status='active')
        db.session.add(new_r)
        target.spots_left -= 1
        db.session.commit()
        flash('Saat değiştirildi ✅', 'success')
        return redirect(url_for('user_dashboard'))

    # GET -> uygun seansları listele (aynı gün veya hocanın belirlediği aralık kriteri istenirse genişletilebilir)
    candidates = (
        Session.query
        .filter(Session.date >= date.today())
        .filter(Session.id != r.session_id)
        .filter(Session.spots_left > 0)
        .order_by(Session.date.asc(), Session.time.asc())
        .all()
    )
    return render_template('move.html', reservation=r, candidates=candidates)

# --- admin iptal ----

@app.route('/admin/reservations/<int:reservation_id>/cancel_refund', methods=['POST'])
@admin_required
def admin_cancel_reservation_refund(reservation_id):
    r = Reservation.query.get_or_404(reservation_id)

    if r.status == 'canceled':
        flash('Rezervasyon zaten iptal.', 'info')
        return redirect(url_for('admin_participants', session_id=r.session_id))

    # iade mantığı
    m = Member.query.filter(func.lower(Member.full_name) == r.user_name.lower()).first()

    # seans tamamlanmış ve kullanıcı attended ise kredi zaten düşmüştür -> geri ver
    if r.status == 'attended' and m:
        m.credits += 1

    # seans tamamlanmadıysa ve rezervasyon aktifse boş yer iade et
    if r.status == 'active' and not r.session.completed:
        r.session.spots_left += 1

    r.status = 'canceled'
    db.session.commit()
    flash('Rezervasyon iptal edildi. (İade uygulandı)', 'success')
    return redirect(url_for('admin_participants', session_id=r.session_id))


# ——— Routes: Admin ———
@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        if pwd == os.getenv('ADMIN_PASSWORD', 'admin'):
            flask_session['is_admin'] = True
            flash('Admin girişi başarılı.', 'success')
            return redirect(url_for('admin_dashboard'))
        flash('Hatalı şifre.', 'error')
    return render_template('admin_login.html')

@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    total_sessions = Session.query.count()
    upcoming = Session.query.filter(Session.date >= date.today()).count()
    active_res = Reservation.query.filter_by(status='active').count()
    today = date.today()
    today_fill = (
        db.session.query(func.sum(Session.capacity - Session.spots_left))
        .filter(Session.date == today)
        .scalar() or 0
    )
    today_cap = (
        db.session.query(func.sum(Session.capacity))
        .filter(Session.date == today)
        .scalar() or 0
    )
    return render_template('admin_dashboard.html',
                           total_sessions=total_sessions,
                           upcoming=upcoming,
                           active_res=active_res,
                           today_fill=today_fill,
                           today_cap=today_cap)

@app.route('/admin/sessions', methods=['GET', 'POST'])
@admin_required
def admin_sessions():
    if request.method == 'POST':
        try:
            d = datetime.strptime(request.form['date'], '%Y-%m-%d').date()
            t = datetime.strptime(request.form['time'], '%H:%M').time()
            cap = int(request.form['capacity'])
            notes = request.form.get('notes') or None
            s = Session(date=d, time=t, capacity=cap, spots_left=cap, notes=notes)
            db.session.add(s)
            db.session.commit()
            flash('Seans eklendi.', 'success')
        except Exception as e:
            db.session.rollback()
            flash('Seans eklenemedi.', 'error')
    sessions = Session.query.order_by(Session.date.asc(), Session.time.asc()).all()
    return render_template('admin_sessions.html', sessions=sessions)

@app.route('/admin/sessions/<int:session_id>/delete', methods=['POST'])
@admin_required
def admin_delete_session(session_id):
    s = Session.query.get_or_404(session_id)
    if s.is_past:
        flash('Geçmiş seans silinemez.', 'error')
        return redirect(url_for('admin_sessions'))

    # --- Katılımcı kredilerini iade et ---
    for r in s.reservations:
        m = Member.query.filter(func.lower(Member.full_name) == r.user_name.lower()).first()
        if m:
            # Eğer attended olmuşsa kredi geri ver
            if r.status == 'attended':
                m.credits += 1
        # Rezervasyonu iptal olarak işaretle
        r.status = 'canceled'
    db.session.commit()
    # --- buraya kadar ---

    db.session.delete(s)
    db.session.commit()
    flash('Seans silindi.', 'success')
    return redirect(url_for('admin_sessions'))


@app.route('/admin/sessions/<int:session_id>/participants')
@admin_required
def admin_participants(session_id):
    s = Session.query.get_or_404(session_id)
    parts = Reservation.query.filter_by(session_id=session_id).order_by(Reservation.created_at.asc()).all()
    return render_template('admin_participants.html', s=s, parts=parts)

@app.route('/admin/members', methods=['GET', 'POST'])
@admin_required
def admin_members():
    if request.method == 'POST':
        name = request.form.get('full_name', '').strip()
        credits = int(request.form.get('credits', 0))
        if not name:
            flash('İsim boş olamaz.', 'error')
            return redirect(url_for('admin_members'))
        canon = Member.canonical(name)
        exists = Member.query.filter(func.lower(Member.full_name) == canon.lower()).first()
        if exists:
            flash('Bu isim zaten kayıtlı.', 'error')
            return redirect(url_for('admin_members'))
        m = Member(full_name=canon, credits=max(0, credits))
        db.session.add(m)
        db.session.commit()
        flash('Üye eklendi.', 'success')
        return redirect(url_for('admin_members'))

    members = Member.query.order_by(Member.full_name.asc()).all()
    return render_template('admin_members.html', members=members)

@app.route('/admin/members/<int:member_id>/delete', methods=['POST'])
@admin_required
def admin_members_delete(member_id):
    m = Member.query.get_or_404(member_id)
    db.session.delete(m)
    db.session.commit()
    flash('Üye silindi.', 'success')
    return redirect(url_for('admin_members'))

@app.route('/admin/members/<int:member_id>/credits', methods=['POST'])
@admin_required
def admin_members_adjust_credits(member_id):
    m = Member.query.get_or_404(member_id)
    delta = int(request.form.get('delta', 0))
    m.credits = max(0, m.credits + delta)
    db.session.commit()
    flash('Seans hakkı güncellendi.', 'success')
    return redirect(url_for('admin_members'))

if __name__ == '__main__':
    app.run(debug=True)