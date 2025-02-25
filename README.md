# H2HDB-Komga

## Description

The `H2HDB-Komga` is a tool that adds tags to comics and renames series in a specified library within Komga. It uses tags from [`H2HDB`](https://github.com/Kuan-Lun/h2hdb) to help you organise and tag your comics and series in the library in Komga.

---

## Installation and Usage

1. Install Python 3.13 or higher from [python.org](https://www.python.org/downloads/).
1. Install the required packages.

    ```bash
    pip install h2hdb-komga
    ```

1. Run the script.

    ```bash
    python -m h2hdb_komga --komgaconfig [komga-config.json] --h2hdbconfig [h2hdb-config.json]
    ```

### Config

#### komga-config.json

```json
{
    "base_url": "[str]", // The url of komga.
    "api_username": "[str]", // The administrator account of komga.
    "api_password": "[str]", // The password of the administrator account of komga.
    "library_id": "[str]" // The libary ID of komga.
}
```

#### h2hdb-config.json

See [Subsection Config in Kuan-Lun/h2hdb](https://github.com/Kuan-Lun/h2hdb#config).

---

## Q & A

- How to use Komga?
See [Rainie's article](https://home.gamer.com.tw/artwork.php?sn=5659465).

- Why aren't the tags for CBZ-files in Komga updated?
When you first run `H2HDB`, it generates CBZ-files. These CBZ-files are not immediately visible in Komga's library. To update them, you have two options: you can either click the 'scan library files' button in Komga, or you can run `H2HDB` twice. The first run scans the library, and the second run updates the tags.

---

## Credits

The project was created by [Kuan-Lun Wang](https://www.klwang.tw/home/).

---

## License

This project is distributed under the terms of the GNU General Public Licence (GPL). For detailed licence terms, see the `LICENSE` file included in this distribution.
