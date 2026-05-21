## Running without installing

Where $REPO_ROOT is the root folder of the cloned repository:

- Set `PYTHONPATH` to `$REPO_ROOT`
- Set `ADUSK_DATA` to `$REPO_ROOT/data`  
  Note that this is technically optional. You could alternately simply copy the contents
  of `$REPO_ROOT/data` to `sys.prefix/share/adusk` (probably `/usr/local/share/adusk`).
- Run `./bin/adusk`

#### In summary:

```
PYTHONPATH="$REPO_ROOT" ADUSK_DATA="$REPO_ROOT/data" ./bin/adusk
```
