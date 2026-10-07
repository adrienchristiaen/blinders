import os
import shutil
import tempfile
import unittest
from pathlib import Path

from blinders.config import Config


def make_repo(root: Path, name: str, readme: str, files: tuple[str, ...] = (), dirs: tuple[str, ...] = ()) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text(readme, encoding="utf-8")
    for f in files:
        (repo / f).write_text("x", encoding="utf-8")
    for d in dirs:
        (repo / d).mkdir()
    return repo


class Sandbox(unittest.TestCase):
    """Isolated config/cache/home directories per test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="blinders-test-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._old = {k: os.environ.get(k) for k in ("BLINDERS_CONFIG_DIR", "BLINDERS_CACHE_DIR", "HOME", "PATH")}
        os.environ["BLINDERS_CONFIG_DIR"] = str(self.tmp / "config")
        os.environ["BLINDERS_CACHE_DIR"] = str(self.tmp / "cache")
        os.environ["HOME"] = str(self.home)
        self.cfg = Config(roots=[str(self.work)])

    def tearDown(self) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sales_repos(self) -> None:
        """An app, a deploy repo that references it, a same-prefix repo, and an unrelated data repo."""
        app = make_repo(self.work, "sales-api-java", "Spring Boot sales API.", dirs=("src",))
        (app / "pom.xml").write_text(
            "<project><parent><artifactId>spring-boot-starter-parent</artifactId></parent>"
            "<artifactId>sales-api-java</artifactId></project>")
        make_repo(self.work, "sales-api", "Python gateway in front of the sales API.", files=("pyproject.toml",))
        k8s = make_repo(self.work, "platform-k8s", "Kubernetes manifests for all services.", dirs=("helm",))
        chart = k8s / "helm" / "sales-api-java"
        chart.mkdir()
        (chart / "Chart.yaml").write_text("name: sales-api-java\nversion: 1.0.0\n")
        (chart / "values.yaml").write_text("image: registry.example.com/team/sales-api-java:1.4\nname: sales-api-java-prod\n")
        etl = make_repo(self.work, "warehouse-etl", "dbt models and airflow dags for the warehouse.", dirs=("dags", "models"))
        (etl / "dbt_project.yml").write_text("name: warehouse\n")

    def standard_repos(self) -> None:
        make_repo(self.work, "carrefour-pipelines",
                  "Airflow DAGs and dbt models loading BigQuery tables from Kafka topics.",
                  files=("dbt_project.yml",), dirs=("dags", "models"))
        make_repo(self.work, "LoopDex", "Catalog of AI technical interviews from candidate reports.",
                  files=("package.json",), dirs=("site",))
        make_repo(self.work, "coolpot", "Passive PCM based personal air cooling module, thermal simulation.",
                  dirs=("cad",))
        make_repo(self.work, "jira-cli", "CLI automating ticket merge request release deploy flow.",
                  files=("pyproject.toml",))


def session_of(out: str) -> Path:
    """Workspace directory from a ``--dry-run`` line ``cd <dir> && <cli> ...``."""
    import shlex
    return Path(shlex.split(out.split(" && ")[0])[1])
