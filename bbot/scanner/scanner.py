import logging
import threading
from uuid import uuid4
import concurrent.futures
from omegaconf import OmegaConf
from collections import OrderedDict

from .target import ScanTarget
from .manager import ScanManager
from .dispatcher import Dispatcher
from bbot.modules import module_loader
from bbot.core.event import make_event
from bbot.core.helpers.helper import ConfigAwareHelper
from bbot.core.helpers.threadpool import ThreadPoolWrapper
from bbot.core.errors import BBOTError, ScanError, ScanCancelledError, ValidationError

log = logging.getLogger("bbot.scanner")


class Scanner:
    def __init__(
        self,
        *targets,
        whitelist=None,
        blacklist=None,
        scan_id=None,
        name=None,
        modules=None,
        output_modules=None,
        config=None,
        dispatcher=None,
        strict_scope=False,
        force_start=False,
    ):
        if modules is None:
            modules = []
        if output_modules is None:
            output_modules = ["human"]
        if config is None:
            config = OmegaConf.create({})
        self.config = config
        self.strict_scope = strict_scope
        self.force_start = force_start

        if scan_id is not None:
            self.id = str(scan_id)
        else:
            self.id = str(uuid4())
        self._status = "NOT_STARTED"

        self.target = ScanTarget(self, *targets, strict_scope=strict_scope)

        self.modules = OrderedDict({})
        self._scan_modules = modules
        self._internal_modules = list(self._internal_modules())
        self._output_modules = output_modules
        self._modules_loaded = False

        self.helpers = ConfigAwareHelper(config=self.config, scan=self)

        if not whitelist:
            self.whitelist = self.target.copy()
        else:
            self.whitelist = ScanTarget(self, *whitelist, strict_scope=strict_scope)
        if not blacklist:
            blacklist = []
        self.blacklist = ScanTarget(self, *blacklist)
        if name is None:
            self.name = str(self.target)
        else:
            self.name = str(name)

        if dispatcher is None:
            self.dispatcher = Dispatcher()
        else:
            self.dispatcher = dispatcher
        self.dispatcher.set_scan(self)

        self.manager = ScanManager(self)

        # prevent too many brute force modules from running at one time
        # because they can bypass the global thread limit
        self.max_brute_forcers = int(self.config.get("max_brute_forcers", 1))
        self._brute_lock = threading.Semaphore(self.max_brute_forcers)

        # Set up thread pools
        max_workers = max(1, self.config.get("max_threads", 100))
        # Shared thread pool, for module use
        self._thread_pool = ThreadPoolWrapper(concurrent.futures.ThreadPoolExecutor(max_workers=max_workers))
        # Event thread pool, for event construction, initialization
        self._event_thread_pool = ThreadPoolWrapper(concurrent.futures.ThreadPoolExecutor(max_workers=max_workers * 2))
        # Internal thread pool, for handle_event(), module setup, cleanup callbacks, etc.
        self._internal_thread_pool = ThreadPoolWrapper(concurrent.futures.ThreadPoolExecutor(max_workers=max_workers))
        self.process_pool = ThreadPoolWrapper(concurrent.futures.ProcessPoolExecutor())

        # scope distance
        self.scope_search_distance = max(0, int(self.config.get("scope_search_distance", 1)))
        self.dns_search_distance = max(
            self.scope_search_distance, int(self.config.get("scope_dns_search_distance", 3))
        )
        self.scope_report_distance = int(self.config.get("scope_report_distance", 1))

        self._prepped = False

    def prep(self):
        if not self._prepped:
            start_msg = f"Scan with {len(self._scan_modules):,} modules seeded with {len(self.target)} targets"
            details = []
            if self.whitelist != self.target:
                details.append(f"{len(self.whitelist):,} in whitelist")
            if self.blacklist:
                details.append(f"{len(self.blacklist):,} in blacklist")
            if details:
                start_msg += f" ({', '.join(details)})"
            self.hugeinfo(start_msg)

            self.load_modules()

            self.info(f"Setting up modules...")
            self.setup_modules()

            self.success(f"Setup succeeded for {len(self.modules):,} modules.")
            self._prepped = True

    def start(self):

        self.prep()

        failed = True

        if not self.target:
            self.warning(f"No scan targets specified")

        try:
            self.status = "STARTING"

            if not self.modules:
                self.error(f"No modules loaded")
                self.status = "FAILED"
                return
            else:
                self.hugesuccess("Starting scan.")

            if self.stopping:
                return

            # distribute seed events
            self.manager.init_events()

            if self.stopping:
                return

            self.status = "RUNNING"
            self.start_modules()
            self.verbose(f"{len(self.modules):,} modules started")

            if self.stopping:
                return

            self.manager.loop_until_finished()
            failed = False

        except KeyboardInterrupt:
            self.stop()
            failed = False

        except ScanCancelledError:
            self.debug("Scan cancelled")

        except ScanError as e:
            self.error(f"{e}")

        except BBOTError as e:
            import traceback

            self.critical(f"Error during scan: {e}")
            self.debug(traceback.format_exc())

        except Exception:
            import traceback

            self.critical(f"Unexpected error during scan:\n{traceback.format_exc()}")

        finally:
            # Shut down thread pools
            self.process_pool.shutdown(wait=True)
            self.helpers.dns._thread_pool.shutdown(wait=True)
            self._event_thread_pool.shutdown(wait=True)
            self._thread_pool.shutdown(wait=True)
            self._internal_thread_pool.shutdown(wait=True)

            if self.status == "ABORTING":
                self.status = "ABORTED"
                self.warning(f"Scan completed with status {self.status}")
            elif failed:
                self.status = "FAILED"
                self.error(f"Scan completed with status {self.status}")
            else:
                self.status = "FINISHED"
                self.success(f"Scan completed with status {self.status}")

            self.dispatcher.on_finish(self)

    def start_modules(self):
        self.verbose(f"Starting module threads")
        for module_name, module in self.modules.items():
            module.start()

    def setup_modules(self, remove_failed=True):
        self.load_modules()
        self.verbose(f"Setting up modules")
        hard_failed = []
        soft_failed = []
        setup_futures = dict()

        for module_name, module in self.modules.items():
            future = self._internal_thread_pool.submit_task(module._setup)
            setup_futures[future] = module_name
        for future in self.helpers.as_completed(setup_futures):
            module_name = setup_futures[future]
            status, msg = future.result()
            if status == True:
                self.debug(f"Setup succeeded for {module_name} ({msg})")
            elif status == False:
                self.error(f"Setup hard-failed for {module_name}: {msg}")
                self.modules[module_name].set_error_state()
                hard_failed.append(module_name)
            else:
                self.warning(f"Setup soft-failed for {module_name}: {msg}")
                soft_failed.append(module_name)
            if not status and remove_failed:
                self.modules.pop(module_name)

        num_output_modules = len([m for m in self.modules.values() if m._type == "output"])
        if num_output_modules < 1:
            raise ScanError("Failed to load output modules. Aborting.")
        total_failed = len(hard_failed + soft_failed)
        if hard_failed:
            msg = f"Setup hard-failed for {len(hard_failed):,} modules ({','.join(hard_failed)})"
            self.fail_setup(msg)
        elif total_failed > 0:
            self.warning(f"Setup failed for {total_failed:,} modules")

    def stop(self, wait=False):
        if self.status != "ABORTING":
            self.status = "ABORTING"
            self.warning(f"Aborting scan")
            for i in range(max(10, self.max_brute_forcers * 10)):
                self._brute_lock.release()
            self.helpers.kill_children()
            self.debug(f"Shutting down thread pools with wait={wait}")
            threads = []
            for pool in [
                self.process_pool,
                self._internal_thread_pool,
                self.helpers.dns._thread_pool,
                self._event_thread_pool,
                self._thread_pool,
            ]:
                t = threading.Thread(target=pool.shutdown, kwargs={"wait": wait, "cancel_futures": True}, daemon=True)
                t.start()
                threads.append(t)
            if wait:
                for t in threads:
                    t.join()
            self.debug("Finished shutting down thread pools")
            self.helpers.kill_children()

    def in_scope(self, e):
        """
        Checks whitelist and blacklist, also taking scope_distance into account
        """
        try:
            e = make_event(e, dummy=True)
        except ValidationError:
            return False
        in_scope = e.scope_distance == 0 or self.whitelisted(e)
        return in_scope and not self.blacklisted(e)

    def blacklisted(self, e):
        e = make_event(e, dummy=True)
        return e in self.blacklist

    def whitelisted(self, e):
        e = make_event(e, dummy=True)
        return e in self.whitelist

    @property
    def word_cloud(self):
        return self.helpers.word_cloud

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, status):
        """
        Block setting after status has been aborted
        """
        if (not self.status == "ABORTING") or (status == "ABORTED"):
            self._status = status
            self.dispatcher.on_status(self._status, self.id)
        else:
            self.debug(f'Attempt to set invalid status "{status}" on aborted scan')

    @property
    def status_detailed(self):
        main_tasks = self._thread_pool.num_tasks
        dns_tasks = self.helpers.dns._thread_pool.num_tasks
        event_tasks = self._event_thread_pool.num_tasks
        internal_tasks = self._internal_thread_pool.num_tasks
        process_tasks = self.process_pool.num_tasks
        total_tasks = main_tasks + dns_tasks + event_tasks + internal_tasks
        status = {
            "queued_tasks": {
                "main": main_tasks,
                "dns": dns_tasks,
                "internal": internal_tasks,
                "process": process_tasks,
                "event": event_tasks,
                "total": total_tasks,
            }
        }
        return status

    def make_event(self, *args, **kwargs):
        kwargs["scan"] = self
        event = make_event(*args, **kwargs)
        return event

    @property
    def log(self):
        if self._log is None:
            self._log = logging.getLogger(f"bbot.agent.scanner")
        return self._log

    @property
    def stopping(self):
        return self.status not in ("STARTING", "RUNNING", "FINISHING")

    @property
    def running(self):
        return self.status in ("STARTING", "RUNNING", "FINISHING")

    @property
    def root_event(self):
        root_event = self.make_event(data=f"SCAN:{self.id}", event_type="SCAN", dummy=True)
        root_event.scope_distance = 0
        root_event._resolved.set()
        root_event.source = root_event
        return root_event

    @property
    def useragent(self):
        return self.config.get("user_agent", "BBOT")

    @property
    def json(self):
        j = dict()
        for i in ("id", "name"):
            v = getattr(self, i, "")
            if v:
                j.update({i: v})
        if self.target:
            j.update({"targets": [str(e.data) for e in self.target]})
        if self.whitelist:
            j.update({"whitelist": [str(e.data) for e in self.whitelist]})
        if self.blacklist:
            j.update({"blacklist": [str(e.data) for e in self.blacklist]})
        if self.modules:
            j.update({"modules": [str(m) for m in self.modules]})
        return j

    def debug(self, *args, **kwargs):
        log.debug(*args, extra={"scan_id": self.id}, **kwargs)

    def verbose(self, *args, **kwargs):
        log.verbose(*args, extra={"scan_id": self.id}, **kwargs)

    def hugeverbose(self, *args, **kwargs):
        log.hugeverbose(*args, extra={"scan_id": self.id}, **kwargs)

    def info(self, *args, **kwargs):
        log.info(*args, extra={"scan_id": self.id}, **kwargs)

    def hugeinfo(self, *args, **kwargs):
        log.hugeinfo(*args, extra={"scan_id": self.id}, **kwargs)

    def success(self, *args, **kwargs):
        log.success(*args, extra={"scan_id": self.id}, **kwargs)

    def hugesuccess(self, *args, **kwargs):
        log.hugesuccess(*args, extra={"scan_id": self.id}, **kwargs)

    def warning(self, *args, **kwargs):
        log.warning(*args, extra={"scan_id": self.id}, **kwargs)

    def hugewarning(self, *args, **kwargs):
        log.hugewarning(*args, extra={"scan_id": self.id}, **kwargs)

    def error(self, *args, **kwargs):
        log.error(*args, extra={"scan_id": self.id}, **kwargs)

    def critical(self, *args, **kwargs):
        log.critical(*args, extra={"scan_id": self.id}, **kwargs)

    def _internal_modules(self):
        speculate = self.config.get("speculate", True)
        excavate = self.config.get("excavate", True)
        if speculate:
            yield "speculate"
        if excavate:
            yield "excavate"

    def load_modules(self):

        if not self._modules_loaded:

            all_modules = list(set(self._scan_modules + self._output_modules + self._internal_modules))
            if not all_modules:
                self.warning(f"No modules to load")
                return

            if not self._scan_modules:
                self.warning(f"No scan modules to load")

            # install module dependencies
            succeeded, failed = self.helpers.depsinstaller.install(
                *self._scan_modules, *self._output_modules, *self._internal_modules
            )
            if failed:
                msg = f"Failed to install dependencies for {len(failed):,} modules: {','.join(failed)}"
                self.fail_setup(msg)
            modules = [m for m in self._scan_modules if m in succeeded]
            output_modules = [m for m in self._output_modules if m in succeeded]
            internal_modules = [m for m in self._internal_modules if m in succeeded]

            # Load scan modules
            self.verbose(f"Loading {len(modules):,} scan modules: {','.join(list(modules))}")
            loaded_modules, failed = self._load_modules(modules)
            self.modules.update(loaded_modules)
            if len(failed) > 0:
                msg = f"Failed to load {len(failed):,} scan modules: {','.join(failed)}"
                self.fail_setup(msg)
            if loaded_modules:
                self.info(
                    f"Loaded {len(loaded_modules):,}/{len(self._scan_modules):,} scan modules ({','.join(list(loaded_modules))})"
                )

            # Load internal modules
            self.verbose(f"Loading {len(internal_modules):,} internal modules: {','.join(list(internal_modules))}")
            loaded_internal_modules, failed_internal = self._load_modules(internal_modules)
            self.modules.update(loaded_internal_modules)
            if len(failed_internal) > 0:
                msg = f"Failed to load {len(loaded_internal_modules):,} internal modules: {','.join(loaded_internal_modules)}"
                self.fail_setup(msg)
            if loaded_internal_modules:
                self.info(
                    f"Loaded {len(loaded_internal_modules):,}/{len(self._internal_modules):,} internal modules ({','.join(list(loaded_internal_modules))})"
                )

            # Load output modules
            self.verbose(f"Loading {len(output_modules):,} output modules: {','.join(list(output_modules))}")
            loaded_output_modules, failed_output = self._load_modules(output_modules)
            self.modules.update(loaded_output_modules)
            if len(failed_output) > 0:
                msg = f"Failed to load {len(failed_output):,} output modules: {','.join(failed_output)}"
                self.fail_setup(msg)
            if loaded_output_modules:
                self.info(
                    f"Loaded {len(loaded_output_modules):,}/{len(self._output_modules):,} output modules, ({','.join(list(loaded_output_modules))})"
                )

            self.modules = OrderedDict(sorted(self.modules.items(), key=lambda x: getattr(x[-1], "_priority", 0)))
            self._modules_loaded = True

    def fail_setup(self, msg):
        msg = str(msg)
        if not self.force_start:
            msg += " (--force to override)"
        if self.force_start:
            self.error(msg)
        else:
            raise ScanError(msg)

    def _load_modules(self, modules):

        modules = [str(m) for m in modules]
        loaded_modules = {}
        failed = set()
        for module_name, module_class in module_loader.load_modules(modules).items():
            if module_class:
                try:
                    loaded_modules[module_name] = module_class(self)
                    self.verbose(f'Loaded module "{module_name}"')
                    continue
                except Exception:
                    import traceback

                    self.warning(f"Failed to load module {module_class}")
                    self.debug(traceback.format_exc())
            else:
                self.warning(f'Failed to load unknown module "{module_name}"')
            failed.add(module_name)
        return loaded_modules, failed
