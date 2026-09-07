import unittest
from datetime import date, timedelta, timezone
from unittest.mock import patch
from urllib.parse import urlparse, parse_qs
import export_inactive_y360_users_with_departments as m

class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2026,8,1)
        self.end = date(2026,8,3)
        self.user = {'createdAt':'2026-01-01T00:00:00Z','departmentId':1}

    def state(self, **changes):
        states = {}
        for day in range(1,4):
            row = dict.fromkeys(m.LAST_USAGE_FIELDS)
            row.update(user_id='1',date=f'2026-08-{day:02d}')
            row.update(changes)
            m.update_statistics_state(states,row,self.start,self.end)
        return states['1']

    def classify(self,state):
        return m.classify(self.user,state,self.start,self.end,timezone.utc)[0]

    def test_empty(self):
        self.assertEqual(self.classify(self.state()),'no_activity_recorded')
    def test_old_and_empty(self):
        self.assertEqual(self.classify(self.state(mail_last_usage_date='2026-07-31')),'inactive')
    def test_cutoff_active(self):
        self.assertEqual(self.classify(self.state(mail_last_usage_date='2026-08-01')),'active')
    def test_received_only(self):
        state=self.state(mail_received_letters_count=9)
        self.assertTrue(state.mail_received)
        self.assertEqual(self.classify(state),'no_activity_recorded')
    def test_new_cutoff(self):
        self.user['createdAt']='2026-08-01T00:00:00Z'
        self.assertEqual(self.classify(self.state()),'new_account')
    def test_created_after(self):
        self.user['createdAt']='2026-09-01T00:00:00Z'
        self.assertEqual(self.classify(self.state()),'excluded')
    def test_missing_creation(self):
        del self.user['createdAt']
        self.assertEqual(self.classify(self.state()),'insufficient_data')
    def test_no_statistics(self):
        self.assertEqual(self.classify(None),'insufficient_data')
    def test_invalid_date(self):
        self.assertEqual(self.classify(self.state(mail_last_usage_date='oops')),'insufficient_data')
    def test_after_report(self):
        self.assertEqual(self.classify(self.state(mail_last_usage_date='2026-09-01')),'insufficient_data')
    def test_incomplete(self):
        state=self.state(); state.row_dates.pop()
        self.assertEqual(self.classify(state),'insufficient_data')
    def test_missing_fields(self):
        states={}
        m.update_statistics_state(states,{'user_id':'1','date':'2026-08-01'},self.start,self.end)
        self.assertIn('missing_usage_field:mail_last_usage_date',states['1'].issues)
    def test_exclusions(self):
        for key,val in [('isRobot',True),('isDismissed',True),('isEnabled',False)]:
            with self.subTest(key=key):
                self.user[key]=val
                self.assertEqual(self.classify(self.state()),'excluded')
                del self.user[key]
    def test_directory_join(self):
        rows,_=m.build_inactive_rows(states={},users={'1':self.user},departments={'1':'Test'},start_date=self.start,end_date=self.end)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['classification'],'insufficient_data')
        self.assertEqual(rows[0]['department_name'],'Test')
    def test_duplicates(self):
        state=self.state()
        states={'1':state}
        m.update_statistics_state(states,dict(dict.fromkeys(m.LAST_USAGE_FIELDS),user_id='1',date='2026-08-01'),self.start,self.end)
        self.assertEqual(self.classify(state),'insufficient_data')
    def test_long_range_and_pagination(self):
        calls=[]
        def fake(url,*args):
            q=parse_qs(urlparse(url).query)
            a=date.fromisoformat(q['start_date'][0]); b=date.fromisoformat(q['end_date'][0])
            calls.append((a,b))
            if 'iteration_key' not in q:
                return {'items':[], 'iteration_key':'next'}
            rows=[]
            while a<=b:
                rows.append(dict(dict.fromkeys(m.LAST_USAGE_FIELDS),user_id='1',date=a.isoformat(),mail_last_usage_date='2026-07-01'))
                a+=timedelta(days=1)
            return {'items':rows,'iteration_key':''}
        with patch.object(m,'request_json',side_effect=fake):
            states,n,p=m.stream_statistics(token='test',org_id='test',start_date=self.start,end_date=date(2026,12,1),limit=1000,timeout=10)
        expected=(date(2026,12,1)-self.start).days+1
        self.assertEqual(n,expected)
        self.assertEqual(len(states['1'].row_dates),expected)
        self.assertFalse(states['1'].issues)
        self.assertTrue(all((b-a).days<28 for a,b in calls))
        self.assertEqual(p,len(calls))

if __name__=='__main__': unittest.main()
