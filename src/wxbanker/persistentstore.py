#    https://launchpad.net/wxbanker
#    persistentstore.py: Copyright 2007-2010 Mike Rooney <mrooney@ubuntu.com>
#
#    This file is part of wxBanker.
#
#    wxBanker is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    wxBanker is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with wxBanker.  If not, see <http://www.gnu.org/licenses/>.

"""
Table: accounts                                v2                 v3              v10
+---------------------------------------------------------------+---------------+-----------------+
| id INTEGER PRIMARY KEY | name VARCHAR(255) | currency INTEGER | balance FLOAT | mintId INTEGER  |
|------------------------+-------------------+------------------+---------------+-----------------|
| 1                      | "My Account"      | 0                | 0             | 123456          |
+---------------------------------------------------------------+---------------+-----------------+

Table: transactions                                                                                       v4
+-------------------------------------------------------------------------------------------------------+----------------+
| id INTEGER PRIMARY KEY | accountId INTEGER | amount FLOAT | description VARCHAR(255) | date CHAR(10)) | linkId INTEGER |
|------------------------+-------------------+--------------+--------------------------+----------------|----------------|
| 1                      | 1                 | 100.00       | "Initial Balance"        | "2007/01/06"   | null           |
+-------------------------------------------------------------------------------------------------------+----------------+
"""
import sys, os, datetime #@UnusedImport
from sqlite3 import dbapi2 as sqlite
import sqlite3
from wx.lib.pubsub import Publisher #@UnresolvedImport

from wxbanker import currencies, debug
from wxbanker.bankobjects.account import Account
from wxbanker.bankobjects.accountlist import AccountList #@UnusedImport
from wxbanker.bankobjects.bankmodel import BankModel
from wxbanker.bankobjects.transaction import Transaction
from wxbanker.bankobjects.transactionlist import TransactionList
from wxbanker.bankobjects.recurringtransaction import RecurringTransaction
from wxbanker.bankobjects.ormobject import ORMKeyValueObject
from wxbanker.bankexceptions import MissingLinkException

class PersistentStore:
    """
    Handles creating the Model (bankobjects) from the store and writing
    back the changes.
    """
    def __init__(self, path, autoSave=True):
        self.Subscriptions = []
        self.Version = 11
        self.Path = path
        self.AutoSave = False
        self.Dirty = False
        self.BatchDepth = 0
        self.cachedModel = None
        # Upgrades can't enable syncing if needed from older versions.
        self.needsSync = False
        existed = True

        # See if the path already exists to decide what to do.
        if not os.path.exists(self.Path):
            existed = False

        # Initialize the connection and optimize it.
        connection = sqlite.connect(self.Path)
        self.dbconn = connection
        # Disable synchronous I/O, which makes everything MUCH faster, at the potential cost of durability.
        self.dbconn.execute("PRAGMA synchronous=OFF;")

        # If the db doesn't exist, initialize it.
        if not existed:
            debug.debug('Initializing', self.Path)
            self.initialize()
        else:
            debug.debug('Loading', self.Path)

        self.Meta = self.getMeta()
        debug.debug(self.Meta)
        while self.Meta['VERSION'] < self.Version:
            # If we are creating a new db, we don't need to backup each iteration.
            self.upgradeDb(self.Meta['VERSION'], backup=existed)
            self.Meta = self.getMeta()
            debug.debug(self.Meta)
         
        # We have to subscribe before syncing otherwise it won't get synced if there aren't other changes.
        self.Subscriptions = (
            (self.onORMObjectUpdated, "ormobject.updated"),
            (self.onAccountBalanceChanged, "account.balance changed"),
            (self.onAccountRemoved, "account.removed"),
            (self.onBatchEvent, "batch"),
            (self.onExit, "exiting"),
        )
        for callback, topic in self.Subscriptions:
            Publisher.subscribe(callback, topic)
            
        # If the upgrade process requires a sync, do so now.
        if self.needsSync:
            self.syncBalances()
            self.needsSync = False

        self.AutoSave = autoSave
        self.commitIfAppropriate()

    def GetModel(self, useCached=True):
        if self.cachedModel is None or not useCached:
            debug.debug('Creating model...')
            self.cachedModel = BankModel(self)

        return self.cachedModel

    def CreateAccount(self, accountName, currency=0):
        if isinstance(currency, currencies.BaseCurrency):
            currency = currencies.GetCurrencyInt(currency)

        if type(currency) != int or currency < 0:
            raise Exception("Currency code must be int and >= 0")

        cursor = self.dbconn.cursor()
        cursor.execute('INSERT INTO accounts (name, currency) VALUES (?, ?)', (accountName, currency))
        ID = cursor.lastrowid
        self.commitIfAppropriate()

        account = Account(self, ID, accountName, currency)
        return account

    def RemoveAccount(self, account):
        self.dbconn.cursor().execute('DELETE FROM accounts WHERE id=?',(account.ID,))
        self.commitIfAppropriate()
        
    def MakeRecurringTransaction(self, recurring):
        cursor = self.dbconn.cursor()
        cursor.execute('INSERT INTO recurring_transactions VALUES (null, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', self.recurringtransaction2result(recurring)[1:])
        self.commitIfAppropriate()
        recurring.ID = cursor.lastrowid
        return recurring

    def MakeTransaction(self, account, transaction):
        cursor = self.dbconn.cursor()
        cursor.execute('INSERT INTO transactions VALUES (null, ?, ?, ?, ?, ?, ?)', [account.ID] + transaction.toResult()[1:])
        self.commitIfAppropriate()
        transaction.ID = cursor.lastrowid
        return transaction

    def RemoveTransaction(self, transaction):
        ID = transaction.ID
        result = self.dbconn.cursor().execute('DELETE FROM transactions WHERE id=?', (ID,)).fetchone() #@UnusedVariable
        self.commitIfAppropriate()
        # The result doesn't appear to be useful here, it is None regardless of whether the DELETE matched anything.
        return True
    
    def RemoveRecurringTransaction(self, recurring):
        ID = recurring.ID
        result = self.dbconn.cursor().execute('DELETE FROM recurring_transactions WHERE id=?', (ID,)) #@UnusedVariable
        self.commitIfAppropriate()

    def Save(self):
        import time; t = time.time()
        self.dbconn.commit()
        debug.debug("Committed in %s seconds" % (time.time()-t))
        self.Dirty = False

    def Close(self):
        self.dbconn.close()
        for callback, topic in self.Subscriptions: #@UnusedVariable
            Publisher.unsubscribe(callback)
            
    def PopulateKeyValues(self, ormkvobj):
        table = ormkvobj.ORM_TABLE
        for result in self.dbconn.cursor().execute("SELECT * from %s" % table).fetchall():
            autoid, key, value = result #@UnusedVariable
            # eval the value since we store it repr'd. However null comes out as None, so cast to a string.
            value = eval(str(value))
            setattr(ormkvobj, key, value)

    def onBatchEvent(self, message):
        batchType = message.topic[1].lower()
        if batchType == "start":
            self.BatchDepth += 1
        elif batchType == "end":
            if self.BatchDepth == 0:
                raise Exception("Cannot end a batch that has not started.")

            self.BatchDepth -= 1
            # If the batching is over, perhaps we should save.
            if self.BatchDepth == 0 and self.Dirty:
                self.commitIfAppropriate()
        else:
            raise Exception("Expected batch type of 'start' or 'end', got '%s'" % batchType)

    def commitIfAppropriate(self):
        # Don't commit if there is a batch in progress.
        if self.AutoSave and not self.BatchDepth:
            self.Save()
        else:
            self.Dirty = True

    def initialize(self):
        cursor = self.dbconn.cursor()

        cursor.execute('CREATE TABLE accounts (id INTEGER PRIMARY KEY, name VARCHAR(255), currency INTEGER)')
        cursor.execute('CREATE TABLE transactions (id INTEGER PRIMARY KEY, accountId INTEGER, amount FLOAT, description VARCHAR(255), date CHAR(10))')

        cursor.execute('CREATE TABLE meta (id INTEGER PRIMARY KEY, name VARCHAR(255), value VARCHAR(255))')
        cursor.execute('INSERT INTO meta VALUES (null, ?, ?)', ('VERSION', '2'))

    def getMeta(self):
        try:
            results = self.dbconn.cursor().execute('SELECT * FROM meta').fetchall()
        except sqlite3.OperationalError:
            meta = {'VERSION': 1}
        else:
            meta = {}
            for uid, key, value in results: #@UnusedVariable
                # All values come in as strings but some we want to cast.
                if key == "VERSION":
                    value = int(value)

                meta[key] = value

        return meta

    def upgradeDb(self, fromVer, backup=True):
        if backup:
            # Make a backup
            source = self.Path
            dest = self.Path + ".backup-v%i-%s" % (fromVer, datetime.date.today().strftime("%Y-%m-%d"))
            debug.debug("Making backup to %s" % dest)
            import shutil
            try:
                shutil.copyfile(source, dest)
            except IOError:
                import traceback; traceback.print_exc()
                raise Exception("Unable to make backup before proceeding with database upgrade...bailing.")

        debug.debug('Upgrading db from %i' % fromVer)
        cursor = self.dbconn.cursor()

        if fromVer == 1:
            # Add `currency` column to the accounts table with default value 0.
            cursor.execute('ALTER TABLE accounts ADD currency INTEGER not null DEFAULT 0')
            # Add metadata table, with version: 2
            cursor.execute('CREATE TABLE meta (id INTEGER PRIMARY KEY, name VARCHAR(255), value VARCHAR(255))')
            cursor.execute('INSERT INTO meta VALUES (null, ?, ?)', ('VERSION', '2'))
        elif fromVer == 2:
            # Add `total` column to the accounts table.
            cursor.execute('ALTER TABLE accounts ADD balance FLOAT not null DEFAULT 0.0')
            # The total column will need to be synced.
            self.needsSync = True
            # Update the meta version number.
        elif fromVer == 3:
            # Add `linkId` column to transactions for transfers.
            cursor.execute('ALTER TABLE transactions ADD linkId INTEGER')
        elif fromVer == 4:
            # Add recurring transactions table.
            transactionBase = "id INTEGER PRIMARY KEY, accountId INTEGER, amount FLOAT, description VARCHAR(255), date CHAR(10)"
            recurringExtra = "repeatType INTEGER, repeatEvery INTEGER, repeatsOn VARCHAR(255), endDate CHAR(10)"
            cursor.execute('CREATE TABLE recurring_transactions (%s, %s)' % (transactionBase, recurringExtra))
        elif fromVer == 5:
            cursor.execute('ALTER TABLE recurring_transactions ADD sourceId INTEGER')
            cursor.execute('ALTER TABLE recurring_transactions ADD lastTransacted CHAR(10)')
        elif fromVer == 6:
            # Handle LP: #496341 by ignoring the error if this table already exists.
            try:
                cursor.execute('ALTER TABLE transactions ADD recurringParent INTEGER')
            except sqlite3.OperationalError:
                debug.debug("Detected a broken database from the 0.4 -> 0.6 upgrade, handling appropriately.")
        elif fromVer == 7:
            # Force a re-sync for the 0.6.1 release after fixing LP: #496341
            self.needsSync = True
        elif fromVer == 8:
            # Add the LastAccountId meta key.
            cursor.execute('INSERT INTO meta VALUES (null, ?, ?)', ('LastAccountId', None))
        elif fromVer == 9:
            # Mint integration
            cursor.execute('INSERT INTO meta VALUES (null, ?, ?)', ('MintEnabled', False))
            cursor.execute('ALTER TABLE accounts ADD mintId INTEGER')
        elif fromVer == 10:
            # The obvious index to create, had I known about indexes previously.
            # It is nice to use IF NOT EXISTS here as some devs and branch users might have it outside of an upgrade.
            cursor.execute('CREATE INDEX IF NOT EXISTS transactions_accountId_idx ON transactions(accountId)')
            # Due to bug LP #605591 there can be orphaned transactions which can cause errors if linked.
            # This is tested by testOrphanedTransactionsAreDeleted (dbupgradetests.DBUpgradeTest)
            # Also takes care of LP #249954 without the explicit need to defensively remove on any account creation.
            self.cleanOrphanedTransactions()
        else:
            raise Exception("Cannot upgrade database from version %i"%fromVer)
        
        """
        # Tagging infrastructure (currently unused due to speed of parsing on startup).
        cursor.execute('CREATE TABLE tags (id INTEGER PRIMARY KEY, name VARCHAR(255))')
        cursor.execute('CREATE TABLE transactions_tags_link (id INTEGER PRIMARY KEY, transactionId INTEGER, tagId INTEGER)')
        cursor.execute('CREATE INDEX transactions_tags_transactionId_idx ON transactions_tags_link(transactionId)')
        cursor.execute('CREATE INDEX transactions_tags_tagId_idx ON transactions_tags_link(tagId)')
        """

        metaVer = fromVer + 1
        cursor.execute('UPDATE meta SET value=? WHERE name=?', (metaVer, "VERSION"))
        self.commitIfAppropriate()

    def syncBalances(self):
        debug.debug("Syncing balances...")
        # Load the model, necessary to sync the balances.
        model = self.GetModel()
        for account in model.Accounts:
            account.Balance = sum([t.Amount for t in account.Transactions])
        self.commitIfAppropriate()
            
    def recurringtransaction2result(self, recurringObj):
        """
        This method converts the Bank's generic implementation of
        a recurring transaction into this model's specific one.
        """
        dateStr = "%s/%s/%s"%(recurringObj.Date.year, str(recurringObj.Date.month).zfill(2), str(recurringObj.Date.day).zfill(2))
        repeatOn = recurringObj.RepeatOn
        if repeatOn:
            repeatOn = ",".join((str(i) for i in repeatOn))
        sourceId = recurringObj.Source and recurringObj.Source.ID
        result = [recurringObj.ID, recurringObj.Parent.ID, recurringObj.Amount, recurringObj.Description, dateStr]
        result += [recurringObj.RepeatType, recurringObj.RepeatEvery, repeatOn, recurringObj.EndDate, sourceId, recurringObj.LastTransacted]
        return result

    def result2account(self, result):
        ID, name, currency, balance, mintId = result
        return Account(self, ID, name, currency, balance, mintId)
    
    def result2recurringtransaction(self, result, parentAccount, allAccounts):
        rId, accountId, amount, description, date, repeatType, repeatEvery, repeatOn, endDate, sourceId, lastTransacted = result #@UnusedVariable
        
        if repeatOn:
            repeatOn = [int(x) for x in repeatOn.split(",")]

        if sourceId:
            sourceAccount = [a for a in allAccounts if a.ID == sourceId][0]
        else:
            sourceAccount = None

        return RecurringTransaction(rId, parentAccount, amount, description, date, repeatType, repeatEvery, repeatOn, endDate, sourceAccount, lastTransacted)

    def getAccountRows(self):
        return self.dbconn.cursor().execute("SELECT * FROM accounts").fetchall()
        
    def GetAccounts(self):
        # Fetch all the accounts.
        accounts = [self.result2account(result) for result in self.getAccountRows()]
        # Add any recurring transactions that exist for each.
        recurrings = self.getRecurringTransactions()
        for recurring in recurrings:
            parentId = recurring[1]
            for account in accounts:
                if account.ID == parentId:
                    rObj = self.result2recurringtransaction(recurring, account, accounts)
                    account.RecurringTransactions.append(rObj)
        return accounts
    
    def getRecurringTransactions(self):
        return self.dbconn.cursor().execute('SELECT * FROM recurring_transactions').fetchall()
        
    def cleanOrphanedTransactions(self):
        # Grab all the accounts that currently exist, to check against.
        accountIDs = [row[0] for row in self.getAccountRows()]
        # We'll keep a list of deceased accounts to clean, so we aren't doing a DELETE per orphan.
        deceasedAccounts = set()
        
        # Iterate over the transactions, treating the cursor as an iterator instead of fetchall() in case there are a lot.
        for transactionRow in self.dbconn.cursor().execute('SELECT * FROM transactions'):
            accountID = transactionRow[1]
            # If the account ID isn't in the current accounts, it must not exist and this transaction is an orphan, so note the parent.
            if accountID not in accountIDs:
                deceasedAccounts.add(accountID)
                
        # Now iterate over the deceased accounts of which to remove orphans.
        for accountID in deceasedAccounts:
            self.dbconn.cursor().execute('DELETE FROM transactions WHERE accountId=?', (accountID,))
        
        self.commitIfAppropriate()        

    def result2transaction(self, result, parentObj, linkedTransaction=None, recurringCache=None):
        tid, pid, amount, description, date, linkId, recurringId = result #@UnusedVariable
        t = Transaction(tid, parentObj, amount, description, date)

        # Handle a linked transaction being passed in, a special case called from a few lines down.
        if linkedTransaction:
            t.LinkedTransaction = linkedTransaction
        else:
            # Handle recurring parents.
            if recurringId:
                t.RecurringParent = recurringCache[recurringId]

            # Handle linked transactions.
            if linkId:
                try:
                    link, linkAccount = self.getTransactionAndParentById(linkId, parentObj, t)
                except MissingLinkException:
                    # The link is gone, it's Account was likely deleted before LP: #514183/605591 was fixed. Remove it.
                    t.LinkedTransaction = None
                else:
                    # If the link parent hasn't loaded its transactions yet, put this in its pre list so this
                    # object is used if and when they are loaded.
                    if linkAccount._Transactions is None:
                        linkAccount._preTransactions.append(link)
                    t.LinkedTransaction = link
                    # Synchronize the RecurringParent attribute.
                    t.LinkedTransaction.RecurringParent = t.RecurringParent
        return t

    def getTransactionsFrom(self, account):
        transactions = TransactionList()
        # Generate a map of recurring transaction IDs to the objects for fast look-up.
        recurringCache = {}
        for recurring in account.Parent.GetRecurringTransactions():
            recurringCache[recurring.ID] = recurring
            
        for result in self.dbconn.cursor().execute('SELECT * FROM transactions WHERE accountId=?', (account.ID,)).fetchall():
            t = self.result2transaction(result, account, recurringCache=recurringCache)
            transactions.append(t)
        return transactions

    def getTransactionAndParentById(self, tId, parentObj, linked):
        result = self.dbconn.cursor().execute('SELECT * FROM transactions WHERE id=? LIMIT 1', (tId,)).fetchone()
        if result is None:
            # This is probably LP #514183: the account of the sibling was deleted.
            raise MissingLinkException("Unable to find the linked transaction")
        
        # Before we can create the LinkedTransaction, we need to find its parent.
        linkedParent = None
        for account in parentObj.Parent:
            if account.ID == result[1]:
                linkedParent = account
                break
        if linkedParent is None:
            raise Exception("Unable to find parent of LinkedTransaction")
        
        transaction = self.result2transaction(result, linkedParent, linkedTransaction=linked)
        return transaction, linkedParent

    def renameAccount(self, oldName, account):
        self.dbconn.cursor().execute("UPDATE accounts SET name=? WHERE name=?", (account.Name, oldName))
        self.commitIfAppropriate()

    def setCurrency(self, currencyIndex):
        self.dbconn.cursor().execute('UPDATE accounts SET currency=?', (currencyIndex,))
        self.commitIfAppropriate()

    def __print__(self):
        cursor = self.dbconn.cursor()
        for account in cursor.execute("SELECT * FROM accounts").fetchall():
            print account[1]
            for trans in cursor.execute("SELECT * FROM transactions WHERE accountId=?", (account[0],)).fetchall():
                print '  -',trans

    def onAccountRenamed(self, message):
        oldName, account = message.data
        self.renameAccount(oldName, account)
        
    def onAccountRemoved(self, message):
        account = message.data
        self.RemoveAccount(account)

    def onAccountBalanceChanged(self, message):
        account = message.data
        self.dbconn.cursor().execute("UPDATE accounts SET balance=? WHERE id=?", (account.Balance, account.ID))
        self.commitIfAppropriate()

    def onExit(self, message):
        if self.Dirty:
            Publisher.sendMessage("warning.dirty exit", message.data)
            
    def onORMObjectUpdated(self, message):
        topic, data = message.topic, message.data
        classname, attrname = topic[-2:]
        ormobj = data
        
        table = ormobj.ORM_TABLE
        value = ormobj.getAttrValue(attrname)
        
        if isinstance(ormobj, ORMKeyValueObject):
            result = self.dbconn.cursor().execute("UPDATE %s SET value=? WHERE name=?" % table, (repr(value), attrname)) #@UnusedVariable
        else:
            # Figure out the name of the column
            colname = attrname.strip("_")
            colname = colname[0].lower() + colname[1:]
            colname = {"repeatOn": "repeatsOn", "source": "sourceId", "linkedTransaction": "linkId"}.get(colname, colname)
            
            query = "UPDATE %s SET %s=? WHERE id=?" % (table, colname)
            objId = ormobj.ID    
            self.dbconn.cursor().execute(query, (value, objId))
            
        self.commitIfAppropriate()
        debug.debug("Persisting %s.%s update (%s)" % (classname, attrname, value))

    def __del__(self):
        self.commitIfAppropriate()
        self.Close()


if __name__ == "__main__":
    import doctest
    doctest.testmod()
