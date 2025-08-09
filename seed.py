# seed.py
from app import app, db, Session
from datetime import date, time as dtime, timedelta

with app.app_context():
    today = date.today()
    demo = [
      (today, dtime(10,0), 6, 'Mat'),
      (today, dtime(18,0), 6, 'Reformer'),
      (today + timedelta(days=1), dtime(10,0), 8, 'Mat'),
    ]
    for d, t, cap, note in demo:
        s = Session(date=d, time=t, capacity=cap, spots_left=cap, notes=note)
        db.session.add(s)
    db.session.commit()
    print("Seed OK")
