# Legacy Tk GUI and command line

> The Tk GUI of the original code still ships with the Python package: `python -m lynxx` without arguments opens it. The desktop app (`app/`, Electron) is the main interface now; its features are described in [specs.md](specs.md) and [operations.md](operations.md). This page keeps the original README sections about the Tk GUI, the command line and the standalone executable build. Tag syntax: [tag-syntax.md](tag-syntax.md).

## How to use
- \[Optional] Choose **Module** (website) to use. The icon in the bottom left corner will change accordingly
- Fill the **Tags** field with tags you want to search for. For base and quick advanced info on tags check **Help -> Tags** section. [More info](tag-syntax.md)
- \[Optional] Configure **filters** to fine-tune your search. You can choose whether you want do download **videos**, **images** or **both**, add **post date** limits, number of **download threads**
- \[Optional] Choose the destination **Path**. Default path is current folder
- Press **Download**

![lynxx](https://github.com/user-attachments/assets/d5deb7c4-b942-4764-a78f-613d6a3df0af)

Note that Lynxx does not restrict your searches to a couple pages or something. You may even search for something like *id:>=0* 
(everything), this won't work though as websites actually put a limit on maximum search depth. For something stupid like this you'll have 
to split your searches using id filter. Plus you may still get banned for abusing the resource. **Lynxx is not a scraping tool**

### Download Options
- *Videos* ‒ some websites serve videos in multiple formats, here you can select a prefered one. You may also exclude videos altogether
- *Images* ‒ some websites serve images in multiple resolutions / quilities (full, preview), which you can choose from. Just like with the videos, you may also filter all the images out
- *Date min / max* ‒ applied to initial search results, format: `dd-mm-yyyy`, ignored if set to default (min: `01-01-1970`, max: `<today>`). Enter some gibberish to reset to default. RX, RN, RP, EN, XB and BB only
- *Parent posts / child posts* ‒ this switch allows to, in addition to initial search result, also download parent posts, all children and all found parents' children even if they don't match the tags you're searching for. RX, EN, XB and BB only
- *Threading* ‒ the number of download threads to use. This also somewhat increases the number of scan threads. More threads means speed, less threads means less network hiccups. Max threads is not a problem in most cases, but you must always remember that nobody likes reckless hammering of their services/APIs
- *Download order* - the order in which found posts will be downloaded. Default is ascending order (lowest id to highest id). Note that sort tags may alter the resulting download order
- *Posts limit* - the maximum number of posts to download. Default is `0` (no limit)

### Misc & Tools
- **File -> Save settings...** \<Ctrl+S> ‒ allows you to save current run parameters to a config file for later or as a template. `Note that only recognized parameters will be loaded - missing parameters will just stay unchanged without any errors given, so if you want to not save some parameters (ex. window position) just remove associated rows from the file`
- **File -> Load settings...** \<Ctrl+O> ‒ load run parameters from previously saved config file. You can also put a `.cfg` or `.json` file into Lynxx folder for autoconfiguration. You have to use one of the following names: ['lynxx', 'auto', 'settings', 'config']
- **File -> Reset all settings** ‒ resets all settings to initial ones. If autoconfigured this will reset to those parameters. Window position is unaffected by this action
- **File -> Open download folder** \<Ctrl+L> ‒ open currently selected destination folder (**Path**), Windows only
- **Edit -> Prefix file names with \<prefix>** ‒ all downloaded file names will be prefixed according to selected module (`rx_`, `rn_`, etc.) so they are not just numbers. This option is enabled by default
- **Edit -> Save tags** ‒ save all parsed post tags to a post info JSON file
- **Edit -> Save source links** ‒ collect and save post source links to a post info JSON file
- **Edit -> Save comments** ‒ collect and save post comments to a post info JSON file
- **Edit -> Save hashes** ‒ parse post hashes and include them in post info JSON
- **Edit -> Save info mode...** ‒ post info file save mode
  - **per run** ‒ create a separate info file per downloader run, this is the default mode
  - **per file** ‒ create a separate info file for every post (not recommended)
  - **merge info lists** ‒ gather and parse existing info files, merge and save all infos together, leaving a single file. **Parsed old info files will be deleted!**
- **Edit -> Extend file names with extra info** ‒ all file names will include short representation of their major tags if any. This may extend resulting full path up to 240 symbols total
- **Edit -> Preserve original modification date** ‒ preserve original file modification date (as read from response header)
- **Edit -> Warn if download folder is not empty** ‒ in GUI mode you will be warned if destination folder is not empty and there is a potential risk of mixing up different search results
- **Edit -> Verbose log** ‒ enable logging of technical messages not related to actual download process. Verbose log is one of the requirements for a proper issue report
- **View -> Log** ‒ open a log window, if you want some readable output
- **View -> Reveal module names** ‒ toggles between module abbreviation and website real name shown in module icon tooltip
- **View -> Hide personal info** ‒ removes all mentions of paths, API key, proxy, etc. from your logs and main window. Useful in case you want to post logs / screenshots (ex. for a bugreport). This option is ignored when checking tags and is not saved to config
- **Connection -> Headers / Cookies** \<F3> ‒ for some websites (RN) and in some other cases you'll have to provide your `cf_clearance` cookie, and the `User-Agent` header has to match the one used in your web browser for target website ‒ to find it while browsing said website open `Web Developer tools -> Network` (or similar), reload the page and check `request headers`
- **Connection -> Set proxy** \<F4> ‒ you can use proxy if you want or if target website is blocked in your country. SOCKS5 proxies are supported too
- **Connection -> Set timeout** \<F5> ‒ override connection timeout if need be
- **Connection -> Set retries count** \<F6> ‒ override connection retries count, may be useful when using a proxy
- **Connection -> API Key** \<F7> ‒ set API authentication info (RX only)
- **Connection -> Download without proxy** ‒ you can use this option if only the main domain of the selected module's website is blocked in your country
- **Connection -> Ignore proxy** ‒ this is just a switch that disables proxy temporarily without wiping it
- **Connection -> Cache processed HTML** ‒ by default HTML is cached as raw bytes, enabling this makes Lynxx cache HTML after it was processed into manageable form - a little bit faster but consumes much more memory. Mainly affects RS module
- **Actions -> Download** \<Ctrl+Shift+D> ‒ same as download button
- **Actions -> Check tags** \<Ctrl+Shift+C> ‒ same as check tags button
- **Actions -> Batch download using tag list...** ‒ read and process tags using a text file. Each line forms a string which then gets put into **Tags** field and downloaded. Warning: download starts immediately! Adjust settings and download options beforehand
- **Actions -> Clear log** \<Ctrl+Shift+E> ‒ same as clear log button
- **Tools -> Query builder** ‒ open a Query Builder tool. Drag & drop links, text files and plain strings into this windows to automatically parse tags and form downloader query, which can then be pushed to the `Tags` field with a single click
- **Tools -> Load from ID list** ‒ load **ID** tag list from a text file. The resulting tags will look like `(id:x~id:y~id:z)` which is an ***OR*** group [expression](tag-syntax.md), effectively allowing you to search for those ids. ~~Broken since about 10.07.2021. Refer to "Broken things" RX forum subsection for details.~~ Re-enabled since version `1.1.284` for all modules using a workaround, but doesn't run in parallel so be aware of that
- **Tools -> Un-tag files...** ‒ renames selected Lynxx-downloaded media files, stripping file names of all extra info
- **Tools -> Re-tag files...** ‒ renames selected Lynxx-downloaded media files, re-appending extra info. You'll need dumped tags info file(s) (see **Edit -> Save tags**)
- **Tools -> Sort files into subfolders...** ‒ a set of tools used to separate downloaded files if need be:
  - **by type** ‒ sort by file type (checking file extension). You can separate files by `videos`/`images`/`flash (RN, EN)` or by extension itself. Note that both `jpeg` and `jpg` files will be placed into **jpg** folder
  - **by size** ‒ sort by file size (you'll have to provide a threshold, in Megabytes). You can use multiple thesholds, separated by space, in any order: `0.5 10 3.0 5.00`
  - **by score** ‒ sort by post score. Make sure that selected files include score in their names or this won't work. You can use multiple thesholds, separated by space, in any order: `100 250 50 500`
- **Tools -> Scan for duplicates...** ‒ download results deduplication tool. Select a folder and scan depth ‒ receive a list of binary equal files. Note that only media files will be scanned. Extra actions depend on selected option:
  - **and report** ‒ do nothing
  - **and separate** ‒ for each set of equal files leave one original file and **move** all its dupes to 'dupes' folder. The longest common path is used as base folder
  - **and remove** ‒ for each set of equal files leave one original file and **delete** all its dupes
- **Tools -> Enable autocompletion** ‒ this feature allows [tag autocompletion](tag-syntax.md#tag-autocompletion) within **Tags** field
- **Tools -> Autocomplete tag...** \<Ctrl+Space> ‒ trigger autocompletion at current cursor position. Note that the hotkey will only work when focusing the **Tags** field
- **Help -> Tags** ‒ a quick list of tag types and how to use them (for selected module)
- **Tags checking** ‒ there is a small button near the **Tags** field. When pressed, Lynxx will try to quickly check if this search yields any results, so this won't work with tags which cannot be passed to website's search engine directly (`AND` group, `OR` groups with meta tags, etc.). As a result the **Tags** field will briefly flash green / red. Additionally, if successful, a window will appear showing the number of results found. Note that this number my be not equal to the files count you'll get downloaded, as date filters, file type filters and related posts filter do not apply during this quick check; when using `favorited_by:X` or `pool:X` special meta tags negative tags also do not apply (except for RN module's `favorited_by` tag where it's supported natively)

## Technical info
Lynxx is written in `Python 3.11`. Lines of code: 14000+. Releases built with `PyInstaller 6.21`

### Logging
Lynxx will log most of its actions, which you can see in **Log** window.  
If any problem occurs it will yield some info unless it's an unexpected fatal error. Lynxx is able to resolve most non-fatal networking errors and IO mishaps, including dropped searches (search overload), non-matching e-tags, file size mismatch, malformed packets, etc.
- **W1**: a minor problem, more of the info
- **W2**: a problem which is going to be fixed, but there is no guarantee it won't occur again
- **W3**: a rather serious problem, Lynxx will attempt to fix it, but it may be not enough, may lead to an error down the road
- **ERROR**: if you see this the download process may fail, Lynxx can only retry the failed action, in most cases that's enough

### Using from console
- It is possible to use Lynxx pre-built executable as a cmdline tool. In main window you will find `Cmd` section ‒ it generates your cmdline arguments every time you make a change ‒ use those arguments as an example. In console window you may need to escape some of them (path, `OR` groups, tags containing dots, etc.). Most arguments are optional though ‒ the only ones required are `tags` (default module is RX):
  - `Lynxx.exe <...args>`

Invoke `Lynxx.exe --help` to list all possible arguments

### Using through python interpreter
- `Python 3.11` or greater is required. See `requirements.txt` for additional dependencies. Install with:
  - `python -m pip install -r requirements.txt`
#### Install as a module
- `cd Lynxx`
- `python -m pip install .`
- `python lynxx [options...]` OR simply
- `lynxx [options...]`
- Running Lynxx without arguments will launch GUI version
#### Without install
- `cd Lynxx`
- Run either:
  - `python lynxx/__main__.py [options...]` OR
  - `lynxx.cmd [options...]` (Windows)
  - `lynxx.sh [options...]` (Linux)
- Running Lynxx without arguments will launch GUI version

Invoke `python lynxx --help` to list all possible arguments

<BR>

## How to build a standalone executable
- **Dependencies:**
  - `Python 3.11` or greater
  - `git`
- Clone the repository:
  - clone the Lynxx repository
- Enter project folder, create virtual environment and install required packages:
  - `cd Lynxx`
  - `python -m venv 1venv`
    - `./1venv/Scripts/activate.ps1` (Windows)
    - `source 1venv/bin/activate` (Linux)
  - `python -m pip install -r requirements.txt`
  - `python -m pip install pyinstaller`
- Run the build script. It will do everything for you and clean up afterwards, your app will be placed into `release` folder:
  - `python lynxx/release`

### Debug build
To make a **Debug** build simply set `__LYNXX_DEBUG__` variable to `True` in `debug.py`. Debug mode enables debug menu, console window display switch, additional logging and a few undocumented features. Some default settings are different in Debug mode
