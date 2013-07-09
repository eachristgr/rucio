# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Vincent Garonne, <vincent.garonne@cern.ch>, 2012-2013
# - Martin Barisits, <martin.barisits@cern.ch>, 2013
# - Mario Lassnig, <mario.lassnig@cern.ch>, 2013

import time

from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.sql.expression import and_, or_

import rucio.core.did

from rucio.common.exception import (InvalidRSEExpression, InvalidReplicationRule, InsufficientQuota,
                                    DataIdentifierNotFound, RuleNotFound, RSENotFound,
                                    ReplicationRuleCreationFailed, InsufficientTargetRSEs)
from rucio.core.lock import get_replica_locks, get_files_and_replica_locks_of_dataset
from rucio.core.monitor import record_timer
from rucio.core.rse import get_and_lock_file_replicas, get_and_lock_file_replicas_for_dataset
from rucio.core.rse_expression_parser import parse_expression
from rucio.core.request import queue_request, cancel_request_did
from rucio.core.rse_selector import RSESelector
from rucio.db import models
from rucio.db.constants import LockState, RuleState, RuleGrouping, DIDReEvaluation, DIDType, RequestType, ReplicaState
from rucio.db.session import read_session, transactional_session


@transactional_session
def add_rule(dids, account, copies, rse_expression, grouping, weight, lifetime, locked, subscription_id, session=None):
    """
    Adds a replication rule for every did in dids

    :param dids:             List of data identifiers.
    :param account:          Account issuing the rule.
    :param copies:           The number of replicas.
    :param rse_expression:   RSE expression which gets resolved into a list of rses.
    :param grouping:         ALL -  All files will be replicated to the same RSE.
                             DATASET - All files in the same dataset will be replicated to the same RSE.
                             NONE - Files will be completely spread over all allowed RSEs without any grouping considerations at all.
    :param weight:           Weighting scheme to be used.
    :param lifetime:         The lifetime of the replication rule.
    :type lifetime:          datetime.timedelta
    :param locked:           If the rule is locked.
    :param subscription_id:  The subscription_id, if the rule is created by a subscription.
    :param session:          The database session in use.
    :returns:                A list of created replication rule ids.
    :raises:                 InvalidReplicationRule, InsufficientQuota, InvalidRSEExpression, DataIdentifierNotFound, ReplicationRuleCreationFailed
    """

    rule_start_time = time.time()

    # 1. Resolve the rse_expression into a list of RSE-ids
    rse_ids = parse_expression(rse_expression, session=session)
    selector = RSESelector(account=account, rse_ids=rse_ids, weight=weight, copies=copies, session=session)

    transfers_to_create = []
    rule_ids = []

    for elem in dids:
        # 2. Get and lock the did
        start_time = time.time()
        try:
            did = session.query(models.DataIdentifier).filter(
                models.DataIdentifier.scope == elem['scope'],
                models.DataIdentifier.name == elem['name'],
                or_(models.DataIdentifier.did_type == DIDType.FILE,
                    models.DataIdentifier.did_type == DIDType.DATASET,
                    models.DataIdentifier.did_type == DIDType.CONTAINER)).with_lockmode('update').one()
        except NoResultFound:
            raise DataIdentifierNotFound('Data identifier %s:%s is not valid.' % (elem['scope'], elem['name']))
        record_timer(stat='rule.lock_did', time=(time.time() - start_time)*1000)
        start_time = time.time()

        # 3. Create the replication rule
        if grouping == 'ALL':
            grouping = RuleGrouping.ALL
        elif grouping == 'NONE':
            grouping = RuleGrouping.NONE
        else:
            grouping = RuleGrouping.DATASET
        new_rule = models.ReplicationRule(account=account, name=elem['name'], scope=elem['scope'], copies=copies, rse_expression=rse_expression, locked=locked, grouping=grouping, expires_at=lifetime, weight=weight, subscription_id=subscription_id)
        try:
            new_rule.save(session=session)
        except IntegrityError, e:
            raise InvalidReplicationRule(e.args[0])
        rule_id = new_rule.id
        rule_ids.append(rule_id)
        record_timer(stat='rule.create_rule', time=(time.time() - start_time)*1000)
        # 4. Resolve the did
        datasetfiles = __resolve_dids_to_locks(did, session=session)
        # 5. Apply the replication rule to create locks and return a list of transfers
        transfers_to_create, locks_ok_cnt, locks_replicating_cnt = __create_locks_for_rule(datasetfiles=datasetfiles,
                                                                                           rseselector=selector,
                                                                                           account=account,
                                                                                           rule_id=rule_id,
                                                                                           copies=copies,
                                                                                           grouping=grouping,
                                                                                           session=session)
        new_rule.locks_ok_cnt = locks_ok_cnt
        new_rule.locks_replicating_cnt = locks_replicating_cnt
        if locks_replicating_cnt == 0:
            new_rule.state = RuleState.OK
        else:
            new_rule.state = RuleState.REPLICATING

    # 6. Create the transfers
    start_time = time.time()
    for transfer in transfers_to_create:
        queue_request(scope=transfer['scope'], name=transfer['name'], dest_rse_id=transfer['rse_id'], req_type=RequestType.TRANSFER, session=session)
    record_timer(stat='rule.queuing_transfers', time=(time.time() - start_time)*1000)
    record_timer(stat='rule.add_rule', time=(time.time() - rule_start_time)*1000)
    return rule_ids


@transactional_session
def add_rules(dids, rules, session=None):
    """
    Adds a list of replication rules to every did in dids

    :params dids:    List of data identifiers.
    :param rules:    List of dictionaries defining replication rules.
                     {account, copies, rse_expression, grouping, weight, lifetime, locked, subscription_id}
    :param session:  The database session in use.
    :returns:        Dictionary (scope, name) with list of created rule ids
    :raises:         InvalidReplicationRule, InsufficientQuota, InvalidRSEExpression, DataIdentifierNotFound, ReplicationRuleCreationFailed
    """

    rule_ids = {}

    for elem in dids:
        rule_ids[(elem['scope'], elem['name'])] = []
        # 1. Get and lock the dids
        try:
            did = session.query(models.DataIdentifier).filter(
                models.DataIdentifier.scope == elem['scope'],
                models.DataIdentifier.name == elem['name'],
                or_(models.DataIdentifier.did_type == DIDType.FILE,
                    models.DataIdentifier.did_type == DIDType.DATASET,
                    models.DataIdentifier.did_type == DIDType.CONTAINER)).with_lockmode('update').one()
        except NoResultFound:
            raise DataIdentifierNotFound('Data identifier %s:%s is not valid.' % (elem['scope'], elem['name']))

        # 2. Resolve the did
        datasetfiles = __resolve_dids_to_locks(did, session=session)

        for rule in rules:
            rule_start_time = time.time()
            # 3. Resolve the rse_expression into a list of RSE-ids
            rse_ids = parse_expression(rule['rse_expression'], session=session)
            selector = RSESelector(account=rule['account'], rse_ids=rse_ids, weight=rule.get('weight'), copies=rule['copies'], session=session)

            # 4. Create the replication rule for every did
            if rule.get('grouping') == 'ALL':
                grouping = RuleGrouping.ALL
            elif rule.get('grouping') == 'NONE':
                grouping = RuleGrouping.NONE
            else:
                grouping = RuleGrouping.DATASET
            new_rule = models.ReplicationRule(account=rule['account'],
                                              name=did.name,
                                              scope=did.scope,
                                              copies=rule['copies'],
                                              rse_expression=rule['rse_expression'],
                                              locked=rule.get('locked'),
                                              grouping=grouping,
                                              expires_at=rule.get('lifetime'),
                                              weight=rule.get('weight'),
                                              subscription_id=rule.get('subscription_id'))
            try:
                new_rule.save(session=session)
            except IntegrityError, e:
                raise InvalidReplicationRule(e.args[0])

            rule_id = new_rule.id
            rule_ids[(did.scope, did.name)].append(rule_id)
            # 5. Apply the replication rule to create locks and return a list of transfers
            transfers_to_create, locks_ok_cnt, locks_replicating_cnt = __create_locks_for_rule(datasetfiles=datasetfiles,
                                                                                               rseselector=selector,
                                                                                               account=rule['account'],
                                                                                               rule_id=rule_id,
                                                                                               copies=rule['copies'],
                                                                                               grouping=grouping,
                                                                                               session=session)
            new_rule.locks_ok_cnt = locks_ok_cnt
            new_rule.locks_replicating_cnt = locks_replicating_cnt
            if locks_replicating_cnt == 0:
                new_rule.state = RuleState.OK
            else:
                new_rule.state = RuleState.REPLICATING

            # 6. Create the transfers
            start_time = time.time()
            for transfer in transfers_to_create:
                queue_request(scope=transfer['scope'], name=transfer['name'], dest_rse_id=transfer['rse_id'], req_type=RequestType.TRANSFER, session=session)

            record_timer(stat='rule.queuing_transfers', time=(time.time() - start_time)*1000)
            record_timer(stat='rule.add_rule', time=(time.time() - rule_start_time)*1000)

    return rule_ids


@transactional_session
def __resolve_dids_to_locks(did, session=None):
    """
    Resolves a did to its constituent childs and reads the locks of all the constituent files.

    :param did:          The db object of the did the rule is applied on.
    :param session:      Session of the db.
    :returns:            datasetfiles dict.
    """

    start_time = time.time()
    datasetfiles = []  # List of Datasets and their files in the Tree [{'scope':, 'name':, 'files':}]
                       # Files are in the format [{'scope': ,'name':, 'bytes':, 'locks': [{'rse_id':, 'state':, 'rule_id':}]}, 'replicas': [SQLALchemy Replica Objects]]

    # a) Resolve the did
    if did.did_type == DIDType.FILE:
        files = [{'scope': did.scope, 'name': did.name, 'bytes': did.bytes, 'locks': get_replica_locks(scope=did.scope, name=did.name, session=session), 'replicas': get_and_lock_file_replicas(scope=did.scope, name=did.name, session=session)}]
        datasetfiles = [{'scope': None, 'name': None, 'files': files}]
    elif did.did_type == DIDType.DATASET:
        files = []
        tmp_locks = get_files_and_replica_locks_of_dataset(scope=did.scope, name=did.name, session=session)
        tmp_replicas = get_and_lock_file_replicas_for_dataset(scope=did.scope, name=did.name, session=session)
        for lock in tmp_locks.values():
            tmp_replicas[(lock['scope'], lock['name'])]['locks'] = lock['locks']
        datasetfiles = [{'scope': did.scope, 'name': did.name, 'files': tmp_replicas.values()}]
    elif did.did_type == DIDType.CONTAINER:
        for dscont in rucio.core.did.list_child_dids(scope=did.scope, name=did.name, lock=True, session=session):
            tmp_locks = get_files_and_replica_locks_of_dataset(scope=dscont['scope'], name=dscont['name'], session=session)
            tmp_replicas = get_and_lock_file_replicas_for_dataset(scope=dscont['scope'], name=dscont['name'], session=session)
            for lock in tmp_locks.values():
                tmp_replicas[(lock['scope'], lock['name'])]['locks'] = lock['locks']
            datasetfiles.append({'scope': dscont['scope'], 'name': dscont['name'], 'files': tmp_replicas.values()})
    else:
        raise InvalidReplicationRule('The did \"%s:%s\" has been deleted.' % (did.scope, did.name))
    record_timer(stat='rule.resolve_dids_to_locks', time=(time.time() - start_time)*1000)
    return datasetfiles


@transactional_session
def __create_locks_for_rule(datasetfiles, rseselector, account, rule_id, copies, grouping, preferred_rse_ids=[], session=None):
    """
    Apply a created replication rule to a did

    :param datasetfiles:       Special dict holding all datasets and files.
    :param rseselector:        The RSESelector to be used.
    :param account:            The account.
    :param rule_id:            The rule_id.
    :param copies:             Number of copies.
    :param grouping:           The grouping to be used.
    :param preferred_rse_ids:  Preferred RSE's to select.
    :param session:            Session of the db.
    :returns:                  (List of transfers to create, #locks_ok_cnt, #locks_replicating_cnt)
    :raises:                   InsufficientQuota, ReplicationRuleCreationFailed
    """

    start_time = time.time()
    locks_to_create = []      # DB Objects
    transfers_to_create = []  # [{'rse_id': rse_id, 'scope': file['scope'], 'name': file['name']}]
    replicas_to_create = []   # DB Objects

    locks_ok_cnt = 0
    locks_replicating_cnt = 0

    if grouping == RuleGrouping.NONE:
        # ########
        # # NONE #
        # ########
        for dataset in datasetfiles:
            for file in dataset['files']:
                if len([lock for lock in file['locks'] if lock['rule_id'] == rule_id]) == copies:
                    # Nothing to do as the file already has the requested amount of locks
                    continue
                if len(preferred_rse_ids) == 0:
                    rse_ids = rseselector.select_rse(file['bytes'], [replica.rse_id for replica in file['replicas']], [replica.rse_id for replica in file['replicas'] if replica.state == ReplicaState.BEING_DELETED])
                else:
                    rse_ids = rseselector.select_rse(file['bytes'], preferred_rse_ids, [replica.rse_id for replica in file['replicas'] if replica.state == ReplicaState.BEING_DELETED])
                for rse_id in rse_ids:
                    replica = [replica for replica in file['replicas'] if replica.rse_id == rse_id]
                    if len(replica) > 0:
                        replica = replica[0]
                        # A replica exists
                        if replica.state == ReplicaState.AVAILABLE:
                            # Replica is fully available
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.OK))
                            file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.OK})
                            replica.lock_cnt += 1
                            replica.tombstone = None
                            locks_ok_cnt += 1
                        else:
                            # Replica is not available at rse yet
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.REPLICATING))
                            file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.REPLICATING})
                            replica.lock_cnt += 1
                            replica.tombstone = None
                            locks_replicating_cnt += 1
                    else:
                        # Replica has to be created
                        locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.REPLICATING))
                        file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.REPLICATING})
                        replica = models.RSEFileAssociation(rse_id=rse_id, scope=file['scope'], name=file['name'], bytes=file['bytes'], lock_cnt=1, state=ReplicaState.UNAVAILABLE)
                        replicas_to_create.append(replica)
                        file['replicas'].append(replica)
                        transfers_to_create.append({'rse_id': rse_id, 'scope': file['scope'], 'name': file['name']})
                        locks_replicating_cnt += 1

    elif grouping == RuleGrouping.ALL:
        # #######
        # # ALL #
        # #######
        bytes = 0
        rse_coverage = {}  # {'rse_id': coverage }
        blacklist = set()
        for dataset in datasetfiles:
            for file in dataset['files']:
                bytes += file['bytes']
                for replica in file['replicas']:
                    if replica.rse_id in rse_coverage:
                        rse_coverage[replica.rse_id] += file['bytes']
                    else:
                        rse_coverage[replica.rse_id] = file['bytes']
                    if replica.state == ReplicaState.BEING_DELETED:
                        blacklist.add(replica.rse_id)
        #TODO add a threshold here?
        if len(preferred_rse_ids) == 0:
            rse_ids = rseselector.select_rse(bytes, [x[0] for x in sorted(rse_coverage.items(), key=lambda tup: tup[1], reverse=True)], list(blacklist))
        else:
            rse_ids = rseselector.select_rse(bytes, preferred_rse_ids, list(blacklist))
        for rse_id in rse_ids:
            for dataset in datasetfiles:
                for file in dataset['files']:
                    if len([lock for lock in file['locks'] if lock['rule_id'] == rule_id]) == copies:
                        continue
                    replica = [replica for replica in file['replicas'] if replica.rse_id == rse_id]
                    if len(replica) > 0:
                        replica = replica[0]
                        # A replica exists
                        if replica.state == ReplicaState.AVAILABLE:
                            # Replica is fully available
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.OK))
                            file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.OK})
                            replica.lock_cnt += 1
                            replica.tombstone = None
                            locks_ok_cnt += 1
                        else:
                            # Replica is not available at rse yet
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.REPLICATING))
                            file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.REPLICATING})
                            replica.lock_cnt += 1
                            replica.tombstone = None
                            locks_replicating_cnt += 1
                    else:
                        # Replica has to be created
                        locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.REPLICATING))
                        file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.REPLICATING})
                        replica = models.RSEFileAssociation(rse_id=rse_id, scope=file['scope'], name=file['name'], bytes=file['bytes'], lock_cnt=1, state=ReplicaState.UNAVAILABLE)
                        replicas_to_create.append(replica)
                        file['replicas'].append(replica)
                        transfers_to_create.append({'rse_id': rse_id, 'scope': file['scope'], 'name': file['name']})
                        locks_replicating_cnt += 1

    else:
        # ###########
        # # DATASET #
        # ###########
        for dataset in datasetfiles:
            bytes = sum([file['bytes'] for file in dataset['files']])
            rse_coverage = {}  # {'rse_id': coverage }
            blacklist = set()
            for file in dataset['files']:
                for replica in file['replicas']:
                    if replica.rse_id in rse_coverage:
                        rse_coverage[replica.rse_id] += file['bytes']
                    else:
                        rse_coverage[replica.rse_id] = file['bytes']
                    if replica.state == ReplicaState.BEING_DELETED:
                        blacklist.add(replica.rse_id)
            if len(preferred_rse_ids) == 0:
                rse_ids = rseselector.select_rse(bytes, [x[0] for x in sorted(rse_coverage.items(), key=lambda tup: tup[1], reverse=True)], list(blacklist))
            else:
                rse_ids = rseselector.select_rse(bytes, preferred_rse_ids, list(blacklist))
            #TODO: Add some threshhold
            for rse_id in rse_ids:
                for file in dataset['files']:
                    if len([lock for lock in file['locks'] if lock['rule_id'] == rule_id]) == copies:
                        continue
                    replica = [replica for replica in file['replicas'] if replica.rse_id == rse_id]
                    if len(replica) > 0:
                        replica = replica[0]
                        # A replica exists
                        if replica.state == ReplicaState.AVAILABLE:
                            # Replica is fully available
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.OK))
                            file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.OK})
                            replica.lock_cnt += 1
                            replica.tombstone = None
                            locks_ok_cnt += 1
                        else:
                            # Replica is not available at rse yet
                            locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.REPLICATING))
                            file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.REPLICATING})
                            replica.lock_cnt += 1
                            replica.tombstone = None
                            locks_replicating_cnt += 1
                    else:
                        # Replica has to be created
                        locks_to_create.append(models.ReplicaLock(rule_id=rule_id, rse_id=rse_id, scope=file['scope'], name=file['name'], account=account, bytes=file['bytes'], state=LockState.REPLICATING))
                        file['locks'].append({'rse_id': rse_id, 'rule_id': rule_id, 'state': LockState.REPLICATING})
                        replica = models.RSEFileAssociation(rse_id=rse_id, scope=file['scope'], name=file['name'], bytes=file['bytes'], lock_cnt=1, state=ReplicaState.UNAVAILABLE)
                        replicas_to_create.append(replica)
                        file['replicas'].append(replica)
                        transfers_to_create.append({'rse_id': rse_id, 'scope': file['scope'], 'name': file['name']})
                        locks_replicating_cnt += 1

    # d) Put the locks to the DB, Put the Replicas in the DBreturn the transfers
    session.add_all(locks_to_create)
    session.add_all(replicas_to_create)
    try:
        session.flush()
    except IntegrityError, e:
        raise ReplicationRuleCreationFailed(e.args[0])

    record_timer(stat='rule.create_locks_for_rule', time=(time.time() - start_time)*1000)
    return(transfers_to_create, locks_ok_cnt, locks_replicating_cnt)


@read_session
def list_rules(filters={}, session=None):
    """
    List replication rules.

    :param filters: dictionary of attributes by which the results should be filtered.
    :param session: The database session in use.
    """

    query = session.query(models.ReplicationRule)
    if filters:
        for (k, v) in filters.items():
            query = query.filter(getattr(models.ReplicationRule, k) == v)

    for rule in query.yield_per(5):
        d = {'id': rule.id,
             'subscription_id': rule.subscription_id,
             'account': rule.account,
             'scope': rule.scope,
             'name': rule.name,
             'state': rule.state,
             'rse_expression': rule.rse_expression,
             'copies': rule.copies,
             'expires_at': rule.expires_at,
             'weight': rule.weight,
             'locked': rule.locked,
             'grouping': rule.grouping,
             'created_at': rule.created_at,
             'updated_at': rule.updated_at}
        yield d


@transactional_session
def delete_expired_rule(session=None):
    """
    Delete all expired replication rules.

    :param session:  The DB Session in use.
    :returns:        True if something expired, false otherwise.
    """

    # Get Rule which needs deletion
    # TODO This needs to skip locks
    rule = session.query(models.ReplicationRule).filter(models.ReplicationRule.expires_at < datetime.utcnow()).with_lockmode('update_nowait').first()
    if rule is None:
        return False
    print 'rule_cleaner: deleting %s' % rule.id
    delete_rule(rule_id=rule.id, session=session)
    return True


@transactional_session
def delete_rule(rule_id, session=None):
    """
    Delete a replication rule.

    :param rule_id: The rule to delete.
    :param session: The database session in use.
    :raises:        RuleNotFound if no Rule can be found.
    """

    start_time = time.time()
    try:
        rule = session.query(models.ReplicationRule).with_lockmode('update').filter_by(id=rule_id).one()
    except NoResultFound:
        raise RuleNotFound('No rule with the id %s found' % (rule_id))

    did = session.query(models.DataIdentifier).filter(
        models.DataIdentifier.scope == rule.scope,
        models.DataIdentifier.name == rule.name,
        or_(models.DataIdentifier.did_type == DIDType.FILE,
            models.DataIdentifier.did_type == DIDType.DATASET,
            models.DataIdentifier.did_type == DIDType.CONTAINER)).with_lockmode('update').one()

    if did.did_type == DIDType.FILE:
        locks = session.query(models.ReplicaLock).filter_by(scope=rule.scope, name=rule.name, rule_id=rule_id).all()
    elif did.did_type == DIDType.DATASET:
        locks = session.query(models.ReplicaLock).join(
            models.DataIdentifierAssociation,
            and_(models.DataIdentifierAssociation.child_scope == models.ReplicaLock.scope,
                 models.DataIdentifierAssociation.child_name == models.ReplicaLock.name)).filter(
                     models.DataIdentifierAssociation.scope == did['scope'],
                     models.DataIdentifierAssociation.name == did['name'],
                     models.ReplicaLock.rule_id == rule_id).all()
    else:
        child_dids = rucio.core.did.list_child_dids(scope=rule.scope, name=rule.name, lock=True, session=session)
        locks = []
        for did in child_dids:
            locks.extend(session.query(models.ReplicaLock).join(
                models.DataIdentifierAssociation,
                and_(models.DataIdentifierAssociation.child_scope == models.ReplicaLock.scope,
                     models.DataIdentifierAssociation.child_name == models.ReplicaLock.name)).filter(
                         models.DataIdentifierAssociation.scope == did['scope'],
                         models.DataIdentifierAssociation.name == did['name'],
                         models.ReplicaLock.rule_id == rule_id).all())

    # Remove locks, set tombstone if applicable
    transfers_to_delete = []  # [{'scope': , 'name':, 'rse_id':}]
    for lock in locks:
        replica = session.query(models.RSEFileAssociation).filter(
            models.RSEFileAssociation.scope == lock.scope,
            models.RSEFileAssociation.name == lock.name,
            models.RSEFileAssociation.rse_id == lock.rse_id).with_lockmode('update').one()
        replica.lock_cnt -= 1
        if lock.state == LockState.REPLICATING and replica.lock_cnt == 0:
            transfers_to_delete.append({'scope': lock.scope, 'name': lock.name, 'rse_id': lock.rse_id})
        lock.delete(session=session)
        if replica.lock_cnt == 0:
            replica.tombstone = datetime.utcnow()

    session.flush()
    rule.delete(session=session)

    for transfer in transfers_to_delete:
        cancel_request_did(scope=transfer['scope'], name=transfer['name'], dest_rse=transfer['rse_id'], req_type=RequestType.TRANSFER)
    record_timer(stat='rule.delete_rule', time=(time.time() - start_time)*1000)


@read_session
def get_rule(rule_id, session=None):
    """
    Get a specific replication rule.

    :param rule_id: The rule_id to select
    :param session: The database session in use.
    :raises:        RuleNotFound if no Rule can be found.
    """

    try:
        rule = session.query(models.ReplicationRule).filter_by(id=rule_id).one()
        d = {}
        for column in rule.__table__.columns:
            d[column.name] = getattr(rule, column.name)
        return d

    except NoResultFound:
        raise RuleNotFound('No rule with the id %s found' % (rule_id))


@transactional_session
def re_evaluate_did(timedeltaseconds=5, session=None):
    """
    Fetches the next did to re-evaluate and re-evaluates it.

    :param timedeltaseconds:  Delay to consider dids for re-evaluation.
    :param session:           The database session in use.
    :returns:                 True if a rule was re-evaluated; False otherwise.
    """

    # Get DID which needs re-evaluation
    # TODO This needs to skip locks
    did = session.query(models.DataIdentifier).filter(
        models.DataIdentifier.rule_evaluation_required < datetime.utcnow() - timedelta(seconds=timedeltaseconds)).with_lockmode('update_nowait').first()
    if did is None:
        return False
    print 're_evaluator: evaluating %s:%s for %s' % (did.scope, did.name, did.rule_evaluation_action)
    if did.rule_evaluation_action == DIDReEvaluation.ATTACH:
        __evaluate_attach(did, session=session)
    elif did.rule_evaluation_action == DIDReEvaluation.DETACH:
        __evaluate_detach(did, session=session)
    else:
        __evaluate_detach(did, session=session)
        __evaluate_attach(did, session=session)
    return True


@transactional_session
def __evaluate_detach(eval_did, session=None):
    """
    Evaluate a parent did which has childs removed

    :param eval_did:  The did object in use.
    :param session:   The database session in use.
    """

    start_time = time.time()
    #Get all parent DID's and row-lock them
    parent_dids = rucio.core.did.list_parent_dids(scope=eval_did.scope, name=eval_did.name, lock=True, session=session)

    #Get all RR from parents and eval_did
    rules = session.query(models.ReplicationRule).filter_by(scope=eval_did.scope, name=eval_did.name).with_lockmode('update').all()
    for did in parent_dids:
        rules.extend(session.query(models.ReplicationRule).filter_by(scope=did['scope'], name=did['name']).with_lockmode('update').all())

    #Get all the files of eval_did
    files = {}
    for file in rucio.core.did.list_files(scope=eval_did.scope, name=eval_did.name, session=session):
        files[(file['scope'], file['name'])] = True

    #Iterate rules and delete locks
    transfers_to_delete = []  # [{'scope': , 'name':, 'rse_id':}]
    for rule in rules:
        query = session.query(models.ReplicaLock).filter_by(rule_id=rule.id).with_lockmode("update")
        for lock in query:
            if (lock.scope, lock.name) not in files:
                replica = session.query(models.RSEFileAssociation).filter(
                    models.RSEFileAssociation.scope == lock.scope,
                    models.RSEFileAssociation.name == lock.name,
                    models.RSEFileAssociation.rse_id == lock.rse_id).with_lockmode('update').one()
                replica.lock_cnt -= 1
                if lock.state == LockState.REPLICATING and replica.lock_cnt == 0:
                    transfers_to_delete.append({'scope': lock.scope, 'name': lock.name, 'rse_id': lock.rse_id})
                session.delete(lock)
                if replica.lock_cnt == 0:
                    replica.tombstone = datetime.utcnow()

    if eval_did.rule_evaluation_action == DIDReEvaluation.BOTH:
        eval_did.rule_evaluation_action = DIDReEvaluation.ATTACH
    else:
        eval_did.rule_evaluation_required = None
        eval_did.rule_evaluation_action = None

    session.flush()

    for transfer in transfers_to_delete:
        cancel_request_did(scope=transfer['scope'], name=transfer['name'], dest_rse=transfer['rse_id'], req_type=RequestType.TRANSFER)
    record_timer(stat='rule.evaluate_did_detach', time=(time.time() - start_time)*1000)


@transactional_session
def __evaluate_attach(eval_did, session=None):
    """
    Evaluate a parent did which has new childs

    :param eval_did:  The did object in use.
    :param session:   The database session in use.
    """

    start_time = time.time()
    #Get all parent DID's and row-lock them
    parent_dids = rucio.core.did.list_parent_dids(scope=eval_did.scope, name=eval_did.name, lock=True, session=session)

    #Get and row-lock immediate child DID's
    always_true = True
    new_child_dids = session.query(models.DataIdentifier).join(models.DataIdentifierAssociation, and_(
        models.DataIdentifierAssociation.child_scope == models.DataIdentifier.scope,
        models.DataIdentifierAssociation.child_name == models.DataIdentifier.name,
        or_(models.DataIdentifier.did_type == DIDType.FILE,
            models.DataIdentifier.did_type == DIDType.DATASET,
            models.DataIdentifier.did_type == DIDType.CONTAINER))).filter(
                models.DataIdentifierAssociation.scope == eval_did.scope,
                models.DataIdentifierAssociation.name == eval_did.name,
                models.DataIdentifierAssociation.rule_evaluation == always_true).with_lockmode('update').all()

    if len(new_child_dids) > 0:
        #Row-Lock all children of the evaluate_dids
        all_child_dscont = []
        if new_child_dids[0].did_type != DIDType.FILE:
            for did in new_child_dids:
                all_child_dscont.extend(rucio.core.did.list_child_dids(scope=did.scope, name=did.name, lock=True, session=session))

        #Get all unsuspended RR from parents and eval_did
        rules = session.query(models.ReplicationRule).filter(
            models.ReplicationRule.scope == eval_did.scope,
            models.ReplicationRule.name == eval_did.name,
            models.ReplicationRule.state != RuleState.SUSPENDED).with_lockmode('update').all()
        for did in parent_dids:
            rules.extend(session.query(models.ReplicationRule).filter(
                models.ReplicationRule.scope == did['scope'],
                models.ReplicationRule.name == did['name'],
                models.ReplicationRule.state != RuleState.SUSPENDED).with_lockmode('update').all())

        #Resolve the new_child_dids to its locks
        if new_child_dids[0].did_type == DIDType.FILE:
            # All the evaluate_dids will be files!
            # Build the special files and datasetfiles object
            files = []
            for did in new_child_dids:
                files.append({'scope': did.scope, 'name': did.name, 'bytes': did.bytes, 'locks': get_replica_locks(scope=did.scope, name=did.name, session=session), 'replicas': get_and_lock_file_replicas(scope=did.scope, name=did.name, session=session)})
            datasetfiles = [{'scope': None, 'name': None, 'files': files}]
        else:
            datasetfiles = []
            for did in new_child_dids:
                datasetfiles.extend(__resolve_dids_to_locks(did, session=session))

        for rule in rules:
            if session.bind.dialect.name != 'sqlite':
                session.begin_nested()
            # 1. Resolve the rse_expression into a list of RSE-ids
            try:
                rse_ids = parse_expression(rule.rse_expression, session=session)
            except (InvalidRSEExpression, RSENotFound) as e:
                rule.state = RuleState.STUCK
                rule.error = str(e)
                rule.save(session=session)
                continue

            # 2. Create the RSE Selector
            try:
                selector = RSESelector(account=rule.account,
                                       rse_ids=rse_ids,
                                       weight=rule.weight,
                                       copies=rule.copies,
                                       session=session)
            except (InvalidRSEExpression, InsufficientTargetRSEs) as e:
                rule.state = RuleState.STUCK
                rule.error = str(e)
                rule.save(session=session)
                continue

            # 3. Apply the Replication rule to the Files
            preferred_rse_ids = []
            # 3.1 Check if the dids in question are files added to a dataset with DATASET/ALL grouping
            if new_child_dids[0].did_type == DIDType.FILE and rule.grouping != RuleGrouping.NONE:
                # Are there any existing did's in this dataset
                always_false = False
                brother_did = session.query(models.DataIdentifierAssociation).filter(
                    models.DataIdentifierAssociation.scope == eval_did.scope,
                    models.DataIdentifierAssociation.name == eval_did.name,
                    models.DataIdentifierAssociation.rule_evaluation == always_false).first()
                if brother_did is not None:
                    # There are other files in the dataset
                    locks = get_replica_locks(scope=brother_did.child_scope,
                                              name=brother_did.child_name,
                                              rule_id=rule.id,
                                              session=session)
                    preferred_rse_ids = [lock['rse_id'] for lock in locks]
            try:
                transfers, locks_ok_cnt, locks_replicating_cnt = __create_locks_for_rule(datasetfiles=datasetfiles,
                                                                                         rseselector=selector,
                                                                                         account=rule.account,
                                                                                         rule_id=rule.id,
                                                                                         copies=rule.copies,
                                                                                         grouping=rule.grouping,
                                                                                         preferred_rse_ids=preferred_rse_ids,
                                                                                         session=session)
                rule.locks_ok_cnt += locks_ok_cnt
                rule.locks_replicating_cnt += locks_replicating_cnt
            except (InsufficientQuota, ReplicationRuleCreationFailed) as e:
                rule.state = RuleState.STUCK
                rule.error = str(e)
                rule.save(session=session)
                continue

            # 4. Create Transfers
            if len(transfers) > 0:
                if rule.locks_stuck_cnt > 0:
                    rule.state = RuleState.STUCK
                else:
                    rule.state = RuleState.REPLICATING
                for transfer in transfers:
                    queue_request(scope=transfer['scope'], name=transfer['name'], dest_rse_id=transfer['rse_id'], req_type=RequestType.TRANSFER, session=session)
            else:
                rule.state = RuleState.OK
            if session.bind.dialect.name != 'sqlite':
                session.commit()

        always_true = True
        session.query(models.DataIdentifierAssociation).filter(
            models.DataIdentifierAssociation.scope == eval_did.scope,
            models.DataIdentifierAssociation.name == eval_did.name,
            models.DataIdentifierAssociation.rule_evaluation == always_true).update({'rule_evaluation': None}, synchronize_session=False)

    # Set the re_evaluation tag to done
    if eval_did.rule_evaluation_action == DIDReEvaluation.BOTH:
        eval_did.rule_evaluation_action = DIDReEvaluation.DETACH
    else:
        eval_did.rule_evaluation_required = None
        eval_did.rule_evaluation_action = None

    session.flush()
    record_timer(stat='rule.evaluate_did_attach', time=(time.time() - start_time)*1000)
