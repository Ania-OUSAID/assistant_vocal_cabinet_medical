import tkinter as tk
from tkinter import ttk, messagebox
import sqlite3, threading, queue, subprocess, os, re, calendar, unicodedata, uuid
from datetime import datetime, date

try:
    import speech_recognition as sr
except ImportError:
    sr = None
try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

DB_NAME = 'cabinet_medical.db'
CABINET_NAME = 'Cabinet Médical Paris Santé'
CABINET_LOCATION = 'Paris, France'
DOCTORS = [
    ('Dr. Sophie Martin', 'Médecine générale'),
    ('Dr. Karim Benali', 'Cardiologie'),
    ('Dr. Claire Dubois', 'Dermatologie'),
    ('Dr. Yacine Haddad', 'Ophtalmologie'),
    ('Dr. Sarah Laurent', 'Pédiatrie'),
]
NEW_PATIENT_WEEKDAY = 2
FOLLOWUP_WEEKDAYS = {0, 1, 3, 4}
TIME_SLOTS = ['09:00','09:30','10:00','10:30','11:00','11:30','14:00','14:30','15:00','15:30','16:00','16:30']
MONTH_NAMES = ['', 'janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre']
MONTH_MAP = {'janvier':1,'fevrier':2,'mars':3,'avril':4,'mai':5,'juin':6,'juillet':7,'aout':8,'septembre':9,'octobre':10,'novembre':11,'decembre':12}
WEEKDAY_NAMES = ['lundi','mardi','mercredi','jeudi','vendredi','samedi','dimanche']

def norm(text):
    text = unicodedata.normalize('NFD', text.lower().strip())
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    return re.sub(r'\s+', ' ', text)

class Database:
    def __init__(self):
        self.conn = sqlite3.connect(DB_NAME, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.create_tables(); self.seed()
    def create_tables(self):
        with self.lock:
            c = self.conn.cursor()
            c.execute('CREATE TABLE IF NOT EXISTS doctors(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,specialty TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS patients(id INTEGER PRIMARY KEY AUTOINCREMENT,first_name TEXT,last_name TEXT,is_existing INTEGER DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
            c.execute('''CREATE TABLE IF NOT EXISTS appointments(
                id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id INTEGER, doctor_id INTEGER,
                appointment_date TEXT, appointment_time TEXT, patient_type TEXT,
                status TEXT DEFAULT 'CONFIRME', created_at TEXT, updated_at TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS interactions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, created_at TEXT,
                speaker TEXT, message TEXT)''')
            self.conn.commit()
    def seed(self):
        with self.lock:
            c = self.conn.cursor()
            if c.execute('SELECT COUNT(*) FROM doctors').fetchone()[0] == 0:
                c.executemany('INSERT INTO doctors(name,specialty) VALUES (?,?)', DOCTORS)
            if c.execute('SELECT COUNT(*) FROM patients').fetchone()[0] == 0:
                c.executemany('INSERT INTO patients(first_name,last_name,is_existing) VALUES (?,?,1)', [
                    ('Nadia','Amrani'),('Youssef','Belaid'),('Leila','Mansouri'),('Anis','Kaci'),('Samira','Bouzid')])
            self.conn.commit()
    def doctors(self):
        with self.lock: return self.conn.execute('SELECT * FROM doctors ORDER BY id').fetchall()
    def find_patient(self, first, last):
        with self.lock:
            return self.conn.execute('SELECT * FROM patients WHERE lower(first_name)=lower(?) AND lower(last_name)=lower(?)',(first,last)).fetchone()
    def create_patient(self, first, last, existing=False):
        with self.lock:
            c=self.conn.cursor(); c.execute('INSERT INTO patients(first_name,last_name,is_existing) VALUES (?,?,?)',(first,last,1 if existing else 0)); self.conn.commit(); return c.lastrowid
    def free(self, doctor_id, d, t, exclude=None):
        with self.lock:
            sql="SELECT id FROM appointments WHERE doctor_id=? AND appointment_date=? AND appointment_time=? AND status='CONFIRME'"
            p=[doctor_id,d,t]
            if exclude: sql+=' AND id<>?'; p.append(exclude)
            return self.conn.execute(sql,p).fetchone() is None
    def slots(self, doctor_id, patient_type, year, month, week, limit=5, exclude=None):
        if week not in (1,2,3,4): return []
        allowed={NEW_PATIENT_WEEKDAY} if patient_type=='nouveau' else FOLLOWUP_WEEKDAYS
        last=calendar.monthrange(year,month)[1]
        start,end={1:(1,7),2:(8,14),3:(15,21),4:(22,last)}[week]
        now=datetime.now(); out=[]
        for day in range(start,end+1):
            d=date(year,month,day)
            if d.weekday() not in allowed: continue
            for t in TIME_SLOTS:
                dt=datetime.strptime(f'{d.isoformat()} {t}','%Y-%m-%d %H:%M')
                if dt<=now: continue
                if self.free(doctor_id,d.isoformat(),t,exclude):
                    out.append((d.isoformat(),t))
                    if len(out)>=limit:return out
        return out
    def book(self,pid,did,d,t,ptype):
        if not self.free(did,d,t): return False
        with self.lock:
            self.conn.execute("INSERT INTO appointments(patient_id,doctor_id,appointment_date,appointment_time,patient_type,status,created_at) VALUES (?,?,?,?,?,'CONFIRME',?)",(pid,did,d,t,ptype,datetime.now().isoformat(timespec='seconds'))); self.conn.commit(); return True
    def upcoming(self,pid):
        with self.lock:
            return self.conn.execute('''SELECT a.*,d.name doctor,d.specialty FROM appointments a JOIN doctors d ON d.id=a.doctor_id
                WHERE a.patient_id=? AND a.status='CONFIRME' AND datetime(a.appointment_date||' '||a.appointment_time)>=datetime('now')
                ORDER BY a.appointment_date,a.appointment_time LIMIT 5''',(pid,)).fetchall()
    def cancel(self,aid):
        with self.lock:
            self.conn.execute("UPDATE appointments SET status='ANNULE',updated_at=? WHERE id=?",(datetime.now().isoformat(timespec='seconds'),aid)); self.conn.commit()
    def modify(self,aid,d,t):
        with self.lock: row=self.conn.execute('SELECT doctor_id FROM appointments WHERE id=?',(aid,)).fetchone()
        if not row or not self.free(row['doctor_id'],d,t,aid): return False
        with self.lock:
            self.conn.execute('UPDATE appointments SET appointment_date=?,appointment_time=?,updated_at=? WHERE id=?',(d,t,datetime.now().isoformat(timespec='seconds'),aid)); self.conn.commit(); return True
    def log(self,call_id,speaker,msg):
        if not call_id:return
        with self.lock:
            self.conn.execute('INSERT INTO interactions(call_id,created_at,speaker,message) VALUES (?,?,?,?)',(call_id,datetime.now().isoformat(timespec='seconds'),speaker,msg)); self.conn.commit()
    def appointments(self):
        with self.lock:
            return self.conn.execute('''SELECT p.first_name||' '||p.last_name patient,d.name doctor,a.appointment_date,a.appointment_time,a.patient_type,a.status
                FROM appointments a JOIN patients p ON p.id=a.patient_id JOIN doctors d ON d.id=a.doctor_id ORDER BY a.appointment_date DESC''').fetchall()
    def patients(self):
        with self.lock:
            return self.conn.execute('''SELECT p.id,p.first_name,p.last_name,p.is_existing,COUNT(a.id) n FROM patients p LEFT JOIN appointments a ON a.patient_id=p.id GROUP BY p.id ORDER BY p.last_name''').fetchall()
    def history(self):
        with self.lock:return self.conn.execute('SELECT * FROM interactions ORDER BY id DESC LIMIT 100').fetchall()
    def stats(self):
        with self.lock:
            c=self.conn.cursor();
            return {
                'patients':c.execute('SELECT COUNT(*) FROM patients').fetchone()[0],
                'appointments':c.execute('SELECT COUNT(*) FROM appointments').fetchone()[0],
                'confirmed':c.execute("SELECT COUNT(*) FROM appointments WHERE status='CONFIRME'").fetchone()[0],
                'cancelled':c.execute("SELECT COUNT(*) FROM appointments WHERE status='ANNULE'").fetchone()[0],
                'interactions':c.execute('SELECT COUNT(*) FROM interactions').fetchone()[0]
            }

class NLU:
    specialties={'generaliste':1,'medecine generale':1,'cardiologue':2,'cardiologie':2,'dermatologue':3,'dermatologie':3,'ophtalmologue':4,'ophtalmologie':4,'pediatre':5,'pediatrie':5}
    doctors={'sophie martin':1,'karim benali':2,'claire dubois':3,'yacine haddad':4,'sarah laurent':5}
    def parse(self,text):
        t=norm(text); r={'intent':None,'doctor':None,'month':None,'week':None}
        if any(x in t for x in ['annuler','annule','supprimer mon rendez vous']): r['intent']='cancel'
        elif any(x in t for x in ['modifier','deplacer','changer mon rendez vous']): r['intent']='modify'
        elif any(x in t for x in ['rendez vous','rdv','reserver','consultation','je voudrais','prendre']): r['intent']='book'
        for k,v in self.doctors.items():
            if k in t:r['doctor']=v;break
        if not r['doctor']:
            for k,v in self.specialties.items():
                if k in t:r['doctor']=v;break
        for k,v in MONTH_MAP.items():
            if k in t:r['month']=v;break
        weeks={'premiere':1,'premier':1,'deuxieme':2,'troisieme':3,'quatrieme':4,'semaine 1':1,'semaine 2':2,'semaine 3':3,'semaine 4':4}
        for k,v in weeks.items():
            if k in t:r['week']=v;break
        m=re.search(r'\b(?:le )?([12]?\d|3[01])\b',t)
        if m and r['month']:
            d=int(m.group(1));r['week']=1 if d<=7 else 2 if d<=14 else 3 if d<=21 else 4
        return r

class Voice:
    def __init__(self,status_cb):
        self.status_cb=status_cb;self.q=queue.Queue();self.cancelled=False;self.proc=None;self.lock=threading.Lock();self.rec=sr.Recognizer() if sr else None
        if self.rec:
            self.rec.dynamic_energy_threshold=True;self.rec.energy_threshold=220;self.rec.pause_threshold=.8
        threading.Thread(target=self.worker,daemon=True).start()
    def worker(self):
        while True:
            text,done,token=self.q.get();
            if self.cancelled:self.q.task_done();continue
            try:
                self.status_cb("🔊 L'assistant parle...")
                if os.name=='nt':
                    env=dict(os.environ);env['TTS_TEXT']=text
                    cmd="Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak($env:TTS_TEXT)"
                    p=subprocess.Popen(['powershell','-NoProfile','-Command',cmd],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
                    with self.lock:self.proc=p
                    p.wait()
                    with self.lock:
                        if self.proc is p:self.proc=None
                elif pyttsx3:
                    e=pyttsx3.init();e.say(text);e.runAndWait();e.stop()
            finally:
                self.status_cb('Prêt');self.q.task_done()
                if done and not self.cancelled:done(token)
    def speak(self,text,done=None,token=None):self.cancelled=False;self.q.put((text,done,token))
    def cancel(self):
        self.cancelled=True
        with self.lock:p=self.proc;self.proc=None
        if p:
            try:p.terminate()
            except:pass
        while True:
            try:self.q.get_nowait();self.q.task_done()
            except queue.Empty:break
    def listen(self,cb,purpose='votre réponse',timeout=10,limit=10):
        if not self.rec:cb(None,"Reconnaissance vocale non installée.");return
        def run():
            try:
                self.status_cb(f'🎤 J\'écoute {purpose}...')
                with sr.Microphone() as source:
                    self.rec.adjust_for_ambient_noise(source,duration=.7)
                    audio=self.rec.listen(source,timeout=timeout,phrase_time_limit=limit)
                self.status_cb('🧠 Analyse de la voix...')
                cb(self.rec.recognize_google(audio,language='fr-FR'),None)
            except sr.WaitTimeoutError:cb(None,'Aucune voix détectée.')
            except sr.UnknownValueError:cb(None,"La phrase n'a pas été comprise.")
            except Exception as e:cb(None,f'Erreur microphone : {e}')
        threading.Thread(target=run,daemon=True).start()

class App(tk.Tk):
    def __init__(self):
        super().__init__();self.title('Assistant vocal intelligent');self.geometry('1240x760');self.minsize(1040,650)
        self.db=Database();self.nlu=NLU();self.voice=Voice(self.set_status);self.doctors=self.db.doctors();self.call_active=False;self.timer_job=None
        self.reset();self.ui();self.refresh()
    def reset(self):
        self.state='idle';self.patient_type=None;self.patient_id=None;self.first='';self.last='';self.doctor=None;self.month=None;self.year=None;self.week=None;self.slots=[];self.slot=None;self.action=None;self.manage=[];self.selected_appt=None;self.pending=None;self.call_id=None;self.token=None;self.started=None;self.voice_retries=0
    def ui(self):
        top=ttk.Frame(self,padding=8);top.pack(fill='x');ttk.Label(top,text=f'🏥 {CABINET_NAME} — {CABINET_LOCATION}',font=('Segoe UI',17,'bold')).pack(side='left');self.status=tk.StringVar(value='Prêt');ttk.Label(top,textvariable=self.status).pack(side='right')
        main=ttk.Panedwindow(self,orient='horizontal');main.pack(fill='both',expand=True,padx=8,pady=(0,8));left=ttk.LabelFrame(main,text='📞 Assistant vocal',padding=8);right=ttk.LabelFrame(main,text='📊 Tableau de bord',padding=8);main.add(left,weight=5);main.add(right,weight=6)
        self.call_lbl=ttk.Label(left,text='Aucun appel en cours',font=('Segoe UI',14,'bold'));self.call_lbl.pack();self.chat=tk.Text(left,height=12,wrap='word',font=('Segoe UI',10),state='disabled');self.chat.pack(fill='both',expand=True)
        self.phone=ttk.LabelFrame(left,text='☎️ Clavier du téléphone',padding=4);self.entry=ttk.Entry(self.phone,font=('Segoe UI',11),justify='center');self.entry.grid(row=0,column=0,columnspan=3,sticky='ew',pady=(0,3));self.entry.bind('<Return>',lambda e:self.send())
        for c in range(3):self.phone.columnconfigure(c,weight=1,uniform='c')
        keys=[('1',1,0),('2',1,1),('3',1,2),('4',2,0),('5',2,1),('6',2,2),('7',3,0),('8',3,1),('9',3,2),('*',4,0),('0',4,1),('#',4,2)]
        for lab,r,c in keys:
            cmd=self.send if lab=='*' else self.clear if lab=='#' else (lambda x=lab:self.entry.insert('end',x))
            tk.Button(self.phone,text=lab,command=cmd,font=('Segoe UI',12,'bold')).grid(row=r,column=c,sticky='nsew',padx=2,pady=2)
        actions=ttk.Frame(left);actions.pack(fill='x',pady=(6,0));self.start_btn=tk.Button(actions,text='📞 Appeler',bg='#2e9d4d',fg='white',command=self.start_call,padx=12,pady=5);self.start_btn.pack(side='left');self.timer=tk.StringVar(value='00:00');ttk.Label(actions,textvariable=self.timer,font=('Segoe UI',12,'bold')).pack(side='left',padx=18);self.hang=tk.Button(actions,text='📵 Raccrocher',bg='#d83b3b',fg='white',command=self.end_call,state='disabled',padx=12,pady=5);self.hang.pack(side='right')
        ttk.Label(left,text="Le micro s'active automatiquement pour le nom et la demande naturelle.\n* = valider • # = supprimer • 4 semaines seulement.",font=('Segoe UI',9)).pack(fill='x',pady=(5,0))
        nb=ttk.Notebook(right);nb.pack(fill='both',expand=True);self.ta=ttk.Frame(nb);self.tp=ttk.Frame(nb);self.ts=ttk.Frame(nb);self.th=ttk.Frame(nb);nb.add(self.ta,text='Rendez-vous');nb.add(self.tp,text='Patients');nb.add(self.ts,text='Statistiques');nb.add(self.th,text='Historique')
        self.at=ttk.Treeview(self.ta,columns=('p','d','date','time','type','status'),show='headings');
        for c,t,w in [('p','Patient',120),('d','Médecin',150),('date','Date',90),('time','Heure',60),('type','Type',70),('status','Statut',80)]:self.at.heading(c,text=t);self.at.column(c,width=w,anchor='center')
        self.at.pack(fill='both',expand=True)
        self.pt=ttk.Treeview(self.tp,columns=('id','name','profile','n'),show='headings');
        for c,t,w in [('id','ID',45),('name','Patient',180),('profile','Profil',100),('n','Nb RDV',80)]:self.pt.heading(c,text=t);self.pt.column(c,width=w,anchor='center')
        self.pt.pack(fill='both',expand=True)
        self.svars={k:tk.StringVar() for k in ['patients','appointments','confirmed','cancelled','interactions']}
        for i,(lab,k) in enumerate([('Patients','patients'),('Rendez-vous','appointments'),('Confirmés','confirmed'),('Annulés','cancelled'),('Interactions','interactions')]):ttk.Label(self.ts,text=lab+':',font=('Segoe UI',11,'bold')).grid(row=i,column=0,sticky='w',padx=10,pady=8);ttk.Label(self.ts,textvariable=self.svars[k],font=('Segoe UI',11)).grid(row=i,column=1,sticky='w',padx=10,pady=8)
        self.ht=ttk.Treeview(self.th,columns=('date','call','speaker','msg'),show='headings');
        for c,t,w in [('date','Date',120),('call','Appel',70),('speaker','Intervenant',80),('msg','Message',350)]:self.ht.heading(c,text=t);self.ht.column(c,width=w,anchor='w')
        self.ht.pack(fill='both',expand=True)
    def set_status(self,t):
        try:self.after(0,lambda:self.status.set(t))
        except:pass
    def start_call(self):
        if self.call_active:return
        self.voice.cancel();self.reset();self.call_active=True;self.call_id=uuid.uuid4().hex[:8];self.token=uuid.uuid4().hex;self.started=datetime.now();self.call_lbl.config(text='📞 Appel en cours');self.start_btn.config(state='disabled');self.hang.config(state='normal');self.phone.pack(fill='x',pady=(5,0),before=self.start_btn.master);self.tick();self.state='ask_patient_type';self.say(f"Bonjour et bienvenue au {CABINET_NAME}. La touche étoile sert à valider et la touche dièse sert à supprimer. Nouveau patient, tapez 1 puis étoile. Patient déjà suivi, tapez 2 puis étoile.")
    def end_call(self):
        if not self.call_active:return
        self.call_active=False;self.voice.cancel();
        if self.timer_job:
            try:self.after_cancel(self.timer_job)
            except:pass
        self.call_lbl.config(text='Aucun appel en cours');self.start_btn.config(state='normal');self.hang.config(state='disabled');self.phone.pack_forget();self.timer.set('00:00');self.state='idle';self.refresh()
    def tick(self):
        if not self.call_active:return
        s=int((datetime.now()-self.started).total_seconds());m,s=divmod(s,60);self.timer.set(f'{m:02d}:{s:02d}');self.timer_job=self.after(1000,self.tick)
    def logchat(self,speaker,text):
        if not self.call_active:return
        self.chat.config(state='normal');self.chat.insert('end',f'{speaker} : {text}\n\n');self.chat.see('end');self.chat.config(state='disabled');self.db.log(self.call_id,'Assistant' if 'Assistant' in speaker else 'Patient',text)
    def say(self,text,after=None):
        if not self.call_active:return
        self.logchat('🤖 Assistant',text);tok=self.token
        cb=None
        if after:
            def cb(rt):
                if self.call_active and rt==self.token==tok:self.after(0,after)
        self.voice.speak(text,cb,tok)
    def patient(self,text):self.logchat('👤 Patient',text)
    def clear(self):self.entry.delete(0,'end')
    def send(self):
        if not self.call_active:return
        t=self.entry.get().strip();
        if not t:return
        self.entry.delete(0,'end');self.patient(t)
        if self.state=='ask_name':self.process_name(t)
        elif self.state=='ask_intent':
            if t in ('1','2','3'):self.route({'1':'book','2':'modify','3':'cancel'}[t],{})
            else:self.process_intent(t)
        else:self.keypad(t)
    def name_listen(self):
        if self.call_active and self.state=='ask_name':self.voice.listen(lambda t,e:self.after(0,lambda:self.name_result(t,e)),'votre prénom et votre nom',10,7)
    def name_result(self,t,e):
        if not self.call_active or self.state!='ask_name':return
        if e:
            self.voice_retries+=1
            if self.voice_retries<=1:self.say("Je n'ai pas entendu votre nom. Dites simplement votre prénom et votre nom.",self.name_listen)
            else:self.say("Je n'arrive pas à reconnaître votre nom. Vous pouvez l'écrire dans la zone de saisie puis appuyer sur Entrée.")
            return
        self.patient(t);self.process_name(t)
    def intent_listen(self):
        if self.call_active and self.state=='ask_intent':self.voice.listen(lambda t,e:self.after(0,lambda:self.intent_result(t,e)),'votre demande',10,12)
    def intent_result(self,t,e):
        if not self.call_active or self.state!='ask_intent':return
        if e:self.say("Je n'ai pas compris. Tapez 1 pour prendre un rendez-vous, 2 pour modifier ou 3 pour annuler.");return
        self.patient(t);self.process_intent(t)
    def extract_name(self,t):
        c=re.sub(r"\b(je m'appelle|je suis|mon nom est|prenom|prénom|nom)\b",' ',t,flags=re.I);c=re.sub(r"[^A-Za-zÀ-ÿ' -]",' ',c);w=[x for x in c.split() if len(x)>1];return None if len(w)<2 else (w[0].capitalize(),' '.join(x.capitalize() for x in w[1:]))
    def process_name(self,t):
        n=self.extract_name(t)
        if not n:self.say("Je n'ai pas compris. Dites par exemple Nadia Amrani.",self.name_listen);return
        self.first,self.last=n;row=self.db.find_patient(self.first,self.last)
        if self.patient_type=='suivi' and row:self.patient_id=row['id'];self.say(f'Merci {self.first} {self.last}. Votre dossier a été retrouvé.')
        elif self.patient_type=='suivi' and not row:self.patient_type='nouveau';self.patient_id=self.db.create_patient(self.first,self.last);self.say("Votre dossier n'a pas été retrouvé. Je poursuis comme nouveau patient.")
        else:self.patient_id=row['id'] if row else self.db.create_patient(self.first,self.last)
        self.ask_intent()
    def ask_intent(self):
        self.state='ask_intent';self.say("Que souhaitez-vous faire ? Vous pouvez dire : je voudrais un cardiologue en octobre deuxième semaine, je veux modifier mon rendez-vous, ou je veux annuler mon rendez-vous. Vous pouvez aussi taper 1 pour prendre, 2 pour modifier ou 3 pour annuler.",self.intent_listen)
    def process_intent(self,t):
        p=self.nlu.parse(t)
        if not p['intent']:self.say("Je n'ai pas compris. Reformulez avec prendre, modifier ou annuler. Sinon tapez 1, 2 ou 3.",self.intent_listen);return
        self.route(p['intent'],p)
    def route(self,intent,p):
        if intent=='book':
            if p.get('doctor'):self.doctor=self.doctors[p['doctor']-1]
            if p.get('month'):self.set_month(p['month'])
            if p.get('week'):self.week=p['week']
            if not self.doctor:self.ask_doctor()
            elif not self.month:self.ask_month()
            elif not self.week:self.ask_week()
            else:self.load_slots()
        else:self.action=intent;self.manage=self.db.upcoming(self.patient_id);self.manage_menu()
    def keypad(self,t):
        m=re.search(r'\d+',t);n=int(m.group()) if m else None
        if self.state=='ask_patient_type':
            if n==1:self.patient_type='nouveau'
            elif n==2:self.patient_type='suivi'
            else:self.say('Tapez 1 ou 2.');return
            self.state='ask_name';self.say('Dites maintenant votre prénom et votre nom.',self.name_listen)
        elif self.state=='choose_doctor':
            if not n or n not in range(1,6):self.say('Tapez un numéro de médecin entre 1 et 5.');return
            self.doctor=self.doctors[n-1];self.ask_month()
        elif self.state=='choose_month':
            if not n or n not in range(1,13):self.say('Tapez un mois entre 1 et 12.');return
            self.set_month(n);self.ask_week()
        elif self.state=='choose_week':
            if n not in (1,2,3,4):self.say('Il y a seulement 4 semaines. Tapez 1, 2, 3 ou 4.');return
            self.week=n;self.load_slots()
        elif self.state=='choose_slot':
            if not n or n>len(self.slots):self.say(f'Tapez un numéro entre 1 et {len(self.slots)}.');return
            self.slot=self.slots[n-1];d,t=self.slot;self.state='confirm_book';self.say(f'Récapitulatif : {self.first} {self.last}, {self.doctor["name"]}, le {self.fdate(d)} à {self.ftime(t)}. Tapez 1 pour confirmer ou 2 pour choisir un autre créneau.')
        elif self.state=='confirm_book':
            if n==1:
                d,t=self.slot
                if self.db.book(self.patient_id,self.doctor['id'],d,t,self.patient_type):self.say(f'Votre rendez-vous est confirmé pour le {self.fdate(d)} à {self.ftime(t)}.');self.state='done';self.refresh()
                else:self.say('Ce créneau vient d’être réservé.');self.load_slots()
            elif n==2:self.state='choose_slot';self.read_slots()
            else:self.say('Tapez 1 ou 2.')
        elif self.state=='no_slots':
            if n==1:self.ask_week()
            elif n==2:self.ask_month()
            else:self.say('Tapez 1 pour une autre semaine ou 2 pour un autre mois.')
        elif self.state=='manage_select':
            if not n or n>len(self.manage):self.say(f'Tapez un numéro entre 1 et {len(self.manage)}.');return
            self.selected_appt=self.manage[n-1]
            if self.action=='cancel':self.state='confirm_cancel';self.say(f'Annuler le rendez-vous du {self.fdate(self.selected_appt["appointment_date"])} à {self.ftime(self.selected_appt["appointment_time"])} ? Tapez 1 pour confirmer ou 2 pour revenir.')
            else:self.doctor=next(d for d in self.doctors if d['id']==self.selected_appt['doctor_id']);self.state='modify_month';self.say('Tapez le numéro du nouveau mois, de 1 à 12.')
        elif self.state=='confirm_cancel':
            if n==1:self.db.cancel(self.selected_appt['id']);self.say('Votre rendez-vous a bien été annulé.');self.state='done';self.refresh()
            elif n==2:self.ask_intent()
            else:self.say('Tapez 1 ou 2.')
        elif self.state=='modify_month':
            if not n or n not in range(1,13):self.say('Tapez un mois de 1 à 12.');return
            self.set_month(n);self.state='modify_week';self.say('Tapez 1, 2, 3 ou 4 pour la nouvelle semaine.')
        elif self.state=='modify_week':
            if n not in (1,2,3,4):self.say('Tapez 1, 2, 3 ou 4.');return
            self.week=n;self.load_modify_slots()
        elif self.state=='modify_slot':
            if not n or n>len(self.slots):self.say(f'Tapez un numéro entre 1 et {len(self.slots)}.');return
            self.pending=self.slots[n-1];d,t=self.pending;self.state='confirm_modify';self.say(f'Nouveau rendez-vous : {self.fdate(d)} à {self.ftime(t)}. Tapez 1 pour confirmer ou 2 pour choisir un autre créneau.')
        elif self.state=='confirm_modify':
            if n==1:
                d,t=self.pending
                if self.db.modify(self.selected_appt['id'],d,t):self.say('Votre rendez-vous a bien été modifié.');self.state='done';self.refresh()
                else:self.say('Ce créneau n’est plus disponible.');self.load_modify_slots()
            elif n==2:self.state='modify_slot';self.read_slots('Nouveaux créneaux')
            else:self.say('Tapez 1 ou 2.')
        elif self.state=='done':self.ask_intent()
    def ask_doctor(self):
        self.state='choose_doctor';parts=[f'Pour {d["name"]}, spécialité {d["specialty"]}, tapez {i} puis étoile.' for i,d in enumerate(self.doctors,1)];self.say('Choisissez le médecin. '+' '.join(parts))
    def ask_month(self):self.state='choose_month';self.say('Choisissez le mois. Tapez 1 pour janvier, 2 février, 3 mars, 4 avril, 5 mai, 6 juin, 7 juillet, 8 août, 9 septembre, 10 octobre, 11 novembre ou 12 décembre, puis étoile.')
    def set_month(self,m):
        now=datetime.now();self.month=m;self.year=now.year if m>=now.month else now.year+1
    def ask_week(self):self.state='choose_week';self.say(f'Vous avez choisi {MONTH_NAMES[self.month]} {self.year}. Tapez 1, 2, 3 ou 4 pour la semaine. La quatrième semaine va du 22 à la fin du mois.')
    def load_slots(self):
        self.slots=self.db.slots(self.doctor['id'],self.patient_type,self.year,self.month,self.week)
        if not self.slots:self.state='no_slots';self.say('Aucun créneau cette semaine. Tapez 1 pour une autre semaine ou 2 pour un autre mois.');return
        self.state='choose_slot';self.read_slots()
    def read_slots(self,prefix='Voici les disponibilités'):
        parts=[f'Pour le {self.fdate(d)} à {self.ftime(t)}, tapez {i} puis étoile.' for i,(d,t) in enumerate(self.slots,1)];self.say(prefix+'. '+' '.join(parts))
    def manage_menu(self):
        if not self.manage:self.state='done';self.say('Je n’ai trouvé aucun rendez-vous confirmé à venir pour ce patient.');return
        self.state='manage_select';verb='modifier' if self.action=='modify' else 'annuler';parts=[f'Rendez-vous {i}, avec {r["doctor"]}, le {self.fdate(r["appointment_date"])} à {self.ftime(r["appointment_time"])} : tapez {i} puis étoile.' for i,r in enumerate(self.manage,1)];self.say(f'Quel rendez-vous souhaitez-vous {verb} ? '+' '.join(parts))
    def load_modify_slots(self):
        self.slots=self.db.slots(self.doctor['id'],self.patient_type,self.year,self.month,self.week,exclude=self.selected_appt['id'])
        if not self.slots:self.state='modify_week';self.say('Aucun nouveau créneau cette semaine. Tapez une autre semaine de 1 à 4.');return
        self.state='modify_slot';self.read_slots('Voici les nouveaux créneaux')
    def refresh(self):
        for tree in [getattr(self,'at',None),getattr(self,'pt',None),getattr(self,'ht',None)]:
            if tree:
                for x in tree.get_children():tree.delete(x)
        if hasattr(self,'at'):
            for r in self.db.appointments():self.at.insert('', 'end', values=(r['patient'],r['doctor'],r['appointment_date'],r['appointment_time'],r['patient_type'],r['status']))
            for r in self.db.patients():self.pt.insert('', 'end', values=(r['id'],f"{r['first_name']} {r['last_name']}",'Suivi' if r['is_existing'] else 'Nouveau',r['n']))
            for r in self.db.history():self.ht.insert('', 'end', values=(r['created_at'][:16],r['call_id'],r['speaker'],r['message']))
            s=self.db.stats();[self.svars[k].set(str(v)) for k,v in s.items()]
    @staticmethod
    def fdate(s):
        d=datetime.strptime(s,'%Y-%m-%d');return f'{WEEKDAY_NAMES[d.weekday()]} {d.day} {MONTH_NAMES[d.month]} {d.year}'
    @staticmethod
    def ftime(s):
        h,m=map(int,s.split(':'));return f'{h} heures' if m==0 else f'{h} heures {m}'

if __name__=='__main__':App().mainloop()
