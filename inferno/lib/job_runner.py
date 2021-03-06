from inferno.lib.disco_ext import get_disco_handle
from inferno.lib.job import InfernoJob
from inferno.lib.rule import extract_subrules, deduplicate_rules, flatten_rules
from disco.error import CommError


def _start_job(rule, settings, urls=None):
    """Start a new job for an InfernoRule

    Note that the output of this function is a tuple of (InfernoJob, DiscoJob)
    If this InfernoJob fails to start by some reasons, e.g. not enough blobs,
    the DiscoJob would be None.
    """
    job = InfernoJob(rule, settings, urls)
    return job, job.start()


def _run_concurrent_rules(rule_list, settings, urls_blackboard):
    """Execute a list of rules concurrently, it assumes all the rules are
    runable(ie. all output urls of its sub_rule are available)

    Output:
        job_results. A dictionary of (rule_name : outputurls) pairs
    Exceptions:
        Exception, if it fails to start a job or one of the jobs dies
    """
    def _get_rule_name(disco_job_name):
        return disco_job_name.rsplit('@')[0]

    # need to save both inferno_jobs and disco_jobs
    jobs = []
    inferno_jobs = []
    for rule in rule_list:
        urls = []
        for sub_rule in extract_subrules(rule):
            urls += urls_blackboard[sub_rule.name]
        inferno_job, job = _start_job(rule, settings, urls)
        if job:
            jobs.append(job)
            inferno_jobs.append(inferno_job)
        else:
            raise Exception('There is not enough blobs to run %s' % rule.name)

    job_results = {}
    stop = False
    server, _ = get_disco_handle(settings.get('server'))
    while jobs:
        try:
            inactive, active = server.results(jobs, 5000)
        except CommError:
            # to deal with the long time jobs(e.g waiting for shuffling)
            continue
        for jobname, (status, results) in inactive:
            if status == "ready":
                job_results[_get_rule_name(jobname)] = results
            elif status == "dead":
                stop = True
        jobs = active
        if stop:
            break

    if stop:
        for jobname, _ in jobs:
            server.kill(jobname)
            raise Exception('One of the concurrent jobs failed.')

    return inferno_jobs, job_results


def _run_sequential_rules(rule_list, settings, urls_blackboard):
    """Execute a list of rules sequentially

    Note that the urls_blackboard could not be updated during the execution,
    since the wait method of InfernoJob does NOT return any urls. If the rule
    needs the results of previous rule, use _run_concurrent_rules instead.
    """
    for rule in rule_list:
        urls = []
        for sub_rule in extract_subrules(rule):
            urls += urls_blackboard[sub_rule.name]
        job, disco_job = _start_job(rule, settings, urls)
        if disco_job:
            job.wait()
        else:
            raise Exception('There is not enough blobs to run %s' % rule.name)


def execute_rule(rule_, settings):
    """Execute an InfernoRule, it handles both single rule and nested rule cases

    * For the single rule, it is executed as usual.
    * For the nested rule, all sub-rules would be executed in a concurrent mode.
      Once all sub-rules are done, run the top-level rule as a single rule.
    """
    def _get_runable_rules(rules, blackboard):
        ready_to_run = []
        for rule in rules:
            ready = True
            for sub_rule in extract_subrules(rule):
                if not blackboard.get(sub_rule.name, None):
                    ready = False
                    break
            if ready:
                ready_to_run.append(rule)

        return ready_to_run

    all_rules = deduplicate_rules(flatten_rules(rule_))
    # initialize the url blackboard, on which each entry is a
    # rule_name : outputurls pair. Default value of outputurls is []
    urls_blackboard = {}
    for rule in all_rules:
        urls_blackboard[rule.name] = []

    # execute all sub-rules concurrently, collect urls for the top-level rule
    parent, sub_rules = all_rules[-1:], all_rules[:-1]
    inferno_jobs = []  # collect all sub-jobs in order to purge them in the end
    while sub_rules:
        runable_rules = _get_runable_rules(sub_rules, urls_blackboard)
        try:
            jobs, ret = _run_concurrent_rules(runable_rules, settings, urls_blackboard)
        except Exception as sub_rule_exception:
            raise sub_rule_exception
        inferno_jobs += jobs
        for key, value in ret.iteritems():
            urls_blackboard[key] = value
        sub_rules = [rule for rule in sub_rules if rule not in runable_rules]

    try:
        _run_sequential_rules(parent, settings, urls_blackboard)
        # InfernoJob will take care about whether purge the sub-jobs or not
        for job in inferno_jobs:
            job._purge(job.job.name)
    except Exception as parent_rule_exception:
        import traceback
        trace = traceback.format_exc(15)
        raise
