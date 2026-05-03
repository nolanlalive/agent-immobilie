import os
import json
import time
import base64
import datetime
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import anthropic
import pickle

# ============================================================
# CONFIGURATION - Remplace ces valeurs avec les tiennes
# ============================================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GMAIL_USER = "nolanlalive@gmail.com"
CHECK_INTERVAL = 60  # vérifie les mails toutes les 60 secondes
RELANCE_JOURS = 3    # relance après 3 jours ouvrables
GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID")  # ID de ton Google Sheet

SCOPES_GMAIL = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.modify'
]

SCOPES_CALENDAR = ['https://www.googleapis.com/auth/calendar']

# ============================================================
# AUTHENTIFICATION GMAIL + CALENDAR
# ============================================================
def get_google_service(service_name, version, scopes):
    creds = None
    token_file = f'token_{service_name}.pickle'
    
    if os.path.exists(token_file):
        with open(token_file, 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, 'wb') as token:
            pickle.dump(creds, token)
    
    return build(service_name, version, credentials=creds)

# ============================================================
# GOOGLE SHEETS - Lire les biens immobiliers
# ============================================================
def get_biens_immobiliers():
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name('service_account.json', scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEETS_ID).sheet1
        biens = sheet.get_all_records()
        return biens
    except Exception as e:
        print(f"Erreur Google Sheets: {e}")
        return []

# ============================================================
# CLAUDE - Analyser le mail et trouver les biens
# ============================================================
def analyser_mail_et_proposer(contenu_mail, biens):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    biens_str = json.dumps(biens, ensure_ascii=False, indent=2)
    
    prompt = f"""Tu es un agent immobilier professionnel en Suisse.

Un client a envoyé ce mail :
{contenu_mail}

Voici notre liste de biens disponibles :
{biens_str}

Analyse les critères du client et sélectionne les 3 biens les plus adaptés.
Rédige un mail de réponse professionnel en français qui :
1. Remercie le client
2. Présente les 3 biens sélectionnés avec leurs caractéristiques
3. Invite le client à répondre s'il est intéressé par une visite
4. Propose des créneaux de visite entre 10h00 et 18h00

Réponds UNIQUEMENT avec le contenu du mail, sans sujet."""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    return message.content[0].text

# ============================================================
# CLAUDE - Détecter si le client est intéressé
# ============================================================
def detecter_interet(contenu_mail):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    prompt = f"""Analyse ce mail d'un client immobilier et réponds UNIQUEMENT par un JSON :

Mail : {contenu_mail}

Réponds avec ce format exact :
{{
  "interesse": true/false,
  "date_souhaitee": "2026-05-10" ou null,
  "heure_souhaitee": "14:00" ou null,
  "bien_choisi": "description du bien" ou null
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    
    try:
        return json.loads(message.content[0].text)
    except:
        return {"interesse": False, "date_souhaitee": None, "heure_souhaitee": None, "bien_choisi": None}

# ============================================================
# GMAIL - Lire les nouveaux mails
# ============================================================
def lire_nouveaux_mails(service, dernier_check):
    query = f'after:{int(dernier_check)} -from:{GMAIL_USER}'
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])
    
    mails = []
    for msg in messages:
        msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        
        headers = msg_data['payload']['headers']
        sujet = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
        expediteur = next((h['value'] for h in headers if h['name'] == 'From'), '')
        
        # Extraire le contenu
        body = ''
        if 'parts' in msg_data['payload']:
            for part in msg_data['payload']['parts']:
                if part['mimeType'] == 'text/plain':
                    body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
        elif 'body' in msg_data['payload']:
            if 'data' in msg_data['payload']['body']:
                body = base64.urlsafe_b64decode(msg_data['payload']['body']['data']).decode('utf-8')
        
        mails.append({
            'id': msg['id'],
            'sujet': sujet,
            'expediteur': expediteur,
            'corps': body,
            'timestamp': msg_data['internalDate']
        })
    
    return mails

# ============================================================
# GMAIL - Envoyer un mail
# ============================================================
def envoyer_mail(service, destinataire, sujet, corps):
    message = MIMEText(corps)
    message['to'] = destinataire
    message['from'] = GMAIL_USER
    message['subject'] = sujet
    
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId='me', body={'raw': raw}).execute()
    print(f"Mail envoyé à {destinataire}")

# ============================================================
# GOOGLE CALENDAR - Créer un RDV de visite
# ============================================================
def creer_rdv_visite(calendar_service, client_email, bien, date_str, heure_str):
    start_datetime = f"{date_str}T{heure_str}:00"
    end_hour = int(heure_str.split(':')[0]) + 1
    end_datetime = f"{date_str}T{end_hour:02d}:00:00"
    
    event = {
        'summary': f'Visite - {bien}',
        'description': f'Visite immobilière avec {client_email}',
        'start': {
            'dateTime': start_datetime,
            'timeZone': 'Europe/Zurich',
        },
        'end': {
            'dateTime': end_datetime,
            'timeZone': 'Europe/Zurich',
        },
        'attendees': [{'email': client_email}],
    }
    
    calendar_service.events().insert(calendarId='primary', body=event).execute()
    print(f"RDV créé pour {client_email} le {date_str} à {heure_str}")

# ============================================================
# CALCUL JOURS OUVRABLES
# ============================================================
def est_jour_ouvrable(date):
    return date.weekday() < 5  # 0=lundi, 4=vendredi

def ajouter_jours_ouvrables(date, jours):
    compteur = 0
    while compteur < jours:
        date += datetime.timedelta(days=1)
        if est_jour_ouvrable(date):
            compteur += 1
    return date

# ============================================================
# AGENT PRINCIPAL
# ============================================================
def run_agent():
    print("🏠 Agent immobilier démarré !")
    
    gmail_service = get_google_service('gmail', 'v1', SCOPES_GMAIL)
    calendar_service = get_google_service('calendar', 'v3', SCOPES_CALENDAR)
    
    # Suivi des mails envoyés (pour les relances)
    # Format: {email_client: {"timestamp": ..., "relance_envoyee": False}}
    suivi = {}
    
    dernier_check = time.time() - 300  # démarre 5 minutes en arrière
    
    while True:
        print(f"🔍 Vérification des mails... ({datetime.datetime.now().strftime('%H:%M:%S')})")
        
        try:
            mails = lire_nouveaux_mails(gmail_service, dernier_check)
            biens = get_biens_immobiliers()
            
            for mail in mails:
                expediteur = mail['expediteur']
                print(f"📧 Nouveau mail de {expediteur}")
                
                # Vérifier si c'est une réponse à une proposition
                if expediteur in suivi:
                    interet = detecter_interet(mail['corps'])
                    
                    if interet['interesse'] and interet['date_souhaitee'] and interet['heure_souhaitee']:
                        # Créer le RDV
                        creer_rdv_visite(
                            calendar_service,
                            expediteur,
                            interet.get('bien_choisi', 'Bien immobilier'),
                            interet['date_souhaitee'],
                            interet['heure_souhaitee']
                        )
                        
                        # Confirmer le RDV par mail
                        confirmation = f"""Bonjour,

Votre visite est confirmée !

📅 Date : {interet['date_souhaitee']}
⏰ Heure : {interet['heure_souhaitee']}
🏠 Bien : {interet.get('bien_choisi', 'le bien sélectionné')}

Vous recevrez également une invitation Google Calendar.

À bientôt,
L'équipe immobilière"""
                        
                        envoyer_mail(gmail_service, expediteur, "✅ Visite confirmée", confirmation)
                        suivi[expediteur]['rdv_cree'] = True
                    
                    # Marquer comme répondu
                    suivi[expediteur]['a_repondu'] = True
                
                else:
                    # Nouveau client - analyser et proposer des biens
                    if biens:
                        reponse = analyser_mail_et_proposer(mail['corps'], biens)
                        envoyer_mail(
                            gmail_service,
                            expediteur,
                            f"Re: {mail['sujet']} - Nos propositions immobilières",
                            reponse
                        )
                        
                        # Enregistrer pour suivi relance
                        suivi[expediteur] = {
                            'timestamp': datetime.datetime.now(),
                            'a_repondu': False,
                            'relance_envoyee': False,
                            'rdv_cree': False
                        }
                    else:
                        print("⚠️ Aucun bien disponible dans Google Sheets")
            
            # Vérifier les relances
            maintenant = datetime.datetime.now()
            for email_client, info in suivi.items():
                if not info['a_repondu'] and not info['relance_envoyee']:
                    date_relance = ajouter_jours_ouvrables(info['timestamp'], RELANCE_JOURS)
                    
                    if maintenant >= date_relance:
                        relance = """Bonjour,

Je me permets de vous relancer suite à notre mail précédent concernant vos recherches immobilières.

Avez-vous eu l'occasion de consulter nos propositions ? Nous serions ravis de vous organiser une visite.

N'hésitez pas à nous répondre directement à ce mail.

Cordialement,
L'équipe immobilière"""
                        
                        envoyer_mail(gmail_service, email_client, "Relance - Vos recherches immobilières", relance)
                        suivi[email_client]['relance_envoyee'] = True
                        print(f"📨 Relance envoyée à {email_client}")
            
            dernier_check = time.time()
        
        except Exception as e:
            print(f"❌ Erreur: {e}")
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_agent()
