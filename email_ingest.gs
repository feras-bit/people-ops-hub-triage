/**
 * People & Ops Hub — Email → Asana ticket ingestion (Google Apps Script).
 *
 * Runs in the peopleops@lap.coffee account. Both Google Groups
 * (hr-questions@ and payroll@) deliver into this mailbox; this script turns
 * each new question email into an Asana ticket in "New (Untriaged)", tagged
 * with the right Category. The existing triage Cloud Function then routes it.
 *
 * SETUP (one time):
 *   1. Sign in as peopleops@lap.coffee → script.google.com → New project → paste this.
 *   2. Project Settings → Script Properties → add: ASANA_PAT = <the Asana token>.
 *   3. Run processInbox once → approve the Gmail + external-request permissions.
 *   4. Triggers (clock icon) → Add trigger: processInbox, time-driven, every 5 minutes.
 *   5. Make sure peopleops@ is a MEMBER of both groups so their mail lands here.
 */

var ASANA_BASE   = 'https://app.asana.com/api/1.0';
var PROJECT      = '1215627193057064';
var SEC_NEW      = '1215627191691478';   // New (Untriaged)
var F_CATEGORY   = '1215681186120906', CAT_HR      = '1215681186120910';
var F_PRIORITY   = '1215688982374676', PRI_MED     = '1215688982374678';
var F_SUBCAT     = '1215681186120915', SUB_PAYROLL = '1215688982374659'; // HR · Payroll question
var F_REQUESTER  = '1215796488510173';

var HR_ADDR      = 'hr-questions@lap.coffee';
var PAYROLL_ADDR = 'payroll@lap.coffee';
var LABEL        = 'Ticketed';

function token_() {
  var t = PropertiesService.getScriptProperties().getProperty('ASANA_PAT');
  if (!t) throw new Error('Missing Script Property ASANA_PAT');
  return t;
}

function asana_(method, path, payload) {
  var res = UrlFetchApp.fetch(ASANA_BASE + path, {
    method: method,
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + token_() },
    payload: payload ? JSON.stringify({ data: payload }) : null,
    muteHttpExceptions: true
  });
  var code = res.getResponseCode();
  if (code < 200 || code >= 300) {
    throw new Error('Asana ' + method + ' ' + path + ' -> ' + code + ': ' + res.getContentText());
  }
  return JSON.parse(res.getContentText());
}

function emailOf_(from) {
  var m = from.match(/<([^>]+)>/);
  return (m ? m[1] : from).trim().toLowerCase();
}

// Which group did this hit? Returns 'payroll', 'hr', or null (not ours).
function streamFor_(msg) {
  var hay = (msg.getTo() + ',' + msg.getCc()).toLowerCase();
  if (hay.indexOf(PAYROLL_ADDR) !== -1) return 'payroll';
  if (hay.indexOf(HR_ADDR) !== -1) return 'hr';
  return null;
}

function processInbox() {
  var label = GmailApp.getUserLabelByName(LABEL) || GmailApp.createLabel(LABEL);
  var threads = GmailApp.search('in:inbox -label:' + LABEL, 0, 50);

  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    try {
      var msg = thread.getMessages()[0];          // original question = first message
      var stream = streamFor_(msg);
      if (!stream) { thread.addLabel(label); continue; }   // not a group email → skip forever

      var sender  = emailOf_(msg.getFrom());
      var subject = (msg.getSubject() || '(no subject)').substring(0, 250);
      var body    = (msg.getPlainBody() || '').substring(0, 5000);

      var cf = {};
      cf[F_CATEGORY]  = CAT_HR;
      cf[F_PRIORITY]  = PRI_MED;                   // emailed questions default to Medium SLA
      cf[F_REQUESTER] = sender;
      if (stream === 'payroll') cf[F_SUBCAT] = SUB_PAYROLL;

      var notes = body + '\n\n— Submitted by email from ' + sender +
                  ' to ' + (stream === 'payroll' ? PAYROLL_ADDR : HR_ADDR);

      var task = asana_('POST', '/tasks',
        { name: subject, notes: notes, projects: [PROJECT], custom_fields: cf });
      asana_('POST', '/sections/' + SEC_NEW + '/addTask', { task: task.data.gid });

      thread.addLabel(label);
      thread.markRead();
    } catch (e) {
      // leave the thread unlabeled so it retries next run; log for inspection
      Logger.log('Failed on thread "' + thread.getFirstMessageSubject() + '": ' + e);
    }
  }
}
